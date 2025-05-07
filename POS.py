
import logging
from typing import List, Optional, Dict
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate,PromptTemplate
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables import RunnablePassthrough, chain
from langchain_core.runnables import RunnableLambda,  RunnableBranch, RunnableSequence, RunnableParallel
from langchain_core.output_parsers import PydanticOutputParser
import aiofiles
from aiocsv import AsyncWriter
import json
import csv
import random
import asyncio
import sys
import os

# Windows 事件循環設定，確保非同步操作（例如檔案寫入、LLM 呼叫）在 Windows 上正常運作
if sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 共用欄位設定
FIELD_LABELS = ['品項', '數量', '單價', '金額', '取餐方式', '取餐號碼', '備註', '外送地址']
FIELD_KEYS = ['item', 'quantity', 'price', 'money', 'dining_option', 'meals_number', 'note', 'address']

# 日誌設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("pos_system.log"), logging.StreamHandler()]
)

class OrderItem(BaseModel):
    item: str = Field(..., alias="品項", description="餐點名稱")
    quantity: int = Field(..., alias="數量", gt=0, description="數量需大於0")
    price: Optional[int] = Field(None, alias="單價", ge=20, le=100, multiple_of=5, description="單價需為20-100的5的倍數")
    money: Optional[int] = Field(None, alias="金額", description="金額")
    dining_option: str = Field(..., alias="取餐方式", pattern="^(內用|外帶|外送)$", description="取餐方式")
    meals_number: Optional[int] = Field(None, alias="取餐號碼", ge=1, description="取餐號碼需≥1")
    note: Optional[str] = Field(None, alias="備註", max_length=50, description="備註最多50字")
    address: Optional[str] = Field(None, alias="外送地址", description="外送地址")

    class Config:
        populate_by_name = True

class BrunchOrder(BaseModel):
    orders: List[OrderItem] = Field(..., alias="訂單", min_items=1, description="至少需一個品項")

    class Config:
        populate_by_name = True

class CustomReportPickupInfo(BaseModel):
    method: str = Field(..., alias="方式", description="取餐方式")
    number: Optional[int] = Field(None, alias="號碼", description="取餐號碼")
    address: Optional[str] = Field(None, alias="地址", description="外送地址")

    class Config:
        populate_by_name = True

class CustomReport(BaseModel):
    items: List[Dict[str, str]] = Field(..., min_items=1, alias="品項", description="品項列表，每項為 {索引: 描述}")
    total_money: int = Field(..., ge=0, alias="總金額", description="訂單總金額")
    pickup_info: CustomReportPickupInfo = Field(..., alias="取餐資訊", description="取餐資訊")

    class Config:
        populate_by_name = True

class POS_Robot:
    meals_counter = 1  # 類別層級計數器
    company = "save_to_csv"
    _price_list = None

    #對類別本身進行操作（透過cls存取）而不是特定的實例
    @classmethod
    def load_price_list(cls, file_path: str = "prices.json"):
        """從 JSON 檔案載入價目表"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                cls._price_list = json.load(f)
            # 驗證價格格式
            for item, price in cls._price_list.items():
                if not (20 <= price <= 100 and price % 5 == 0):
                    raise ValueError(f"商品 {item} 價格 {price} 不符合格式要求")
            logging.info(f"已從 {file_path} 載入 {len(cls._price_list)} 項商品價格")
        except Exception as e:
            logging.critical(f"價格載入失敗: {str(e)}")
            raise

    @classmethod
    def get_price(cls, item: str) -> int:
        """帶安全檢查的價格獲取方法"""
        if not cls._price_list:
            raise ValueError("價格表未初始化，請先呼叫 load_price_list()")
            
        price = cls._price_list.get(item)
        if price is None:
            raise KeyError(f"商品 {item} 不存在於價格表中")
        return price

    def __init__(self, price_file: str = "prices.json"):
        current_dir = os.path.dirname(os.path.abspath(__file__))
        settings_path = os.path.join(current_dir, 'settings.json')
        """初始化時載入settings.json"""
        try:
            with open(settings_path, 'r', encoding='utf-8') as f:
                settings = json.load(f)
            self.company = settings.get('company', 'save_to_csv')
        except FileNotFoundError:
            logging.warning(f"settings.json 不存在（路徑：{settings_path}），使用預設值 'save_to_csv'")
            self.company = 'save_to_csv'
        except json.JSONDecodeError:
            logging.warning(f"settings.json 格式錯誤（路徑：{settings_path}），使用預設值 'save_to_csv'")
            self.company = 'save_to_csv'
        if self.company not in ['save_to_csv', 'save_to_json', 'save_to_custom_json', 'All']:
            logging.warning(f"無效的 company 設定：{self.company}，使用預設值 'save_to_csv'")
            self.company = 'save_to_csv'
        logging.info(f"載入 company 設定：{self.company}")
        POS_Robot.company = self.company

        price_file = os.path.join(current_dir, price_file)
        """初始化時載入價格"""
        if POS_Robot._price_list is None:
            POS_Robot.load_price_list(price_file)
        elif price_file != "prices.json":  # 允許動態切換價格文件
            POS_Robot.load_price_list(price_file)
        
        self.llm = ChatOpenAI(
            model="gpt-4o-mini",
            temperature=0,
            openai_api_key=os.getenv("OPENAI_API_KEY")
        )
        self.full_chain = self._build_dynamic_chain()
        self.processing_chain = self._build_post_processing_chain()
        self.whole_chain = self._build_whole_chain()

    async def process_order(self, user_input: str) -> BrunchOrder:
        """整合動態提示的訂單處理"""
        try:
            # Use whole_chain to process input and post-process in one step
            processed_result = await self.whole_chain.ainvoke({"input": user_input})
                
            self.visualize_chain()
            return processed_result
        except Exception as e:
            logging.error(f"訂單處理失敗：{str(e)}", exc_info=True)
            raise

    def _build_whole_chain(self):
        async def save_result(data: BrunchOrder) -> BrunchOrder:
            try:
                if self.company == 'save_to_csv':
                    await self.save_to_csv(data)
                elif self.company == 'save_to_json':
                    await self.save_to_json(data)
                elif self.company == 'save_to_custom_json':
                    await self.save_to_custom_json(data)
                elif self.company == 'All':
                    await self.save_to_standard_formats(data)
                logging.info(f"儲存完成，使用 {self.company} 方式")
            except Exception as e:
                logging.error(f"儲存失敗：{str(e)}")
                raise
            return data

        return (
            self.full_chain
            | self.processing_chain
            | RunnableLambda(save_result)
        )


    def _build_dynamic_chain(self):
        parser = PydanticOutputParser(pydantic_object=BrunchOrder)
        prompt_template = ChatPromptTemplate.from_template(
            """
            根據以下點餐內容，執行以下步驟：
            1. 判斷訂單類型（外送、客製、一般）：
            - 外送：包含外送地址且明確提及“外送”。
            - 客製：包含客製化需求（如“加”、“不要”、“切邊”、“微糖”）。
            - 一般：其他情況。
            2. 處理訂單：
            - 將所有飲料變體標準化為「紅茶」、「奶茶」、「咖啡」。
            - 將溫度、甜度、去冰等需求填入備註。
            - 吐司和三明治的額外需求也填入備註。
            - 根據訂單類型，特別注意：
                - 外送：確保外送地址正確填入。
                - 客製：仔細處理備註中的客製化需求。
                - 一般：按標準流程處理。

            點餐內容：{input}

            輸出結構化 JSON，格式為：
            {{
            "orders": [
                {{
                "item": "餐點名稱",
                "quantity": 數量（整數，大於0）,
                "dining_option": "內用|外帶|外送",
                "address": "外送地址（若無則為空字符串）",
                "note": "備註（若無則為空字符串）",
                "price": null,
                "money": null,
                "meals_number": null
                }},
                ...
            ],
            "total_money": 總金額（整數，大於等於0）
            }}
            確保每個訂單包含 "item", "quantity", "dining_option", "address", "note"，且 "price", "money", "meals_number" 設為 null。
            {format_instructions}
            """
        ).partial(format_instructions=parser.get_format_instructions())

        processing_chain = prompt_template | self.llm.with_structured_output(BrunchOrder, method="function_calling")
        return processing_chain
            
    def _build_post_processing_chain(self):
        def check_prices(data: BrunchOrder) -> BrunchOrder:
                    """檢查所有品項是否都有定義的價格"""
                    required_items = set(item.item for item in data.orders)
                    missing_items = required_items - set(POS_Robot._price_list.keys())
                    if missing_items:
                        raise ValueError(f"以下商品未定義價格: {', '.join(missing_items)}")
                    return data
        return (
                    RunnableLambda(check_prices)  # 價格預檢查
                    | RunnableParallel(          # 並行處理價格和取餐號碼
                        prices=RunnableLambda(self._auto_generate_prices),
                        meals_number=RunnableLambda(self._increment_meals_number)
                    )
                    | RunnableLambda(self._merge_results)  # 合併結果
                )
  
    def _merge_results(self, results: dict) -> BrunchOrder:
        prices_result = results["prices"]
        meals_number_result = results["meals_number"]
        updated_orders = [
            OrderItem(
                item=item.item,
                quantity=item.quantity,
                price=item.price,
                money=item.money,
                dining_option=item.dining_option,
                meals_number=meals_number_result.orders[0].meals_number,
                note=item.note,
                address=item.address
            )
            for item in prices_result.orders
        ]
        return BrunchOrder(orders=updated_orders)
        
    def _auto_generate_prices(self, data: BrunchOrder) -> BrunchOrder:
        """基於檔案價格的自動補全"""
        updated_orders = [
            OrderItem(
                item=item.item,
                quantity=item.quantity,
                price=self.get_price(item.item) if item.price is None else item.price,
                money=(self.get_price(item.item) if item.price is None else item.price) * item.quantity,
                dining_option=item.dining_option,
                meals_number=item.meals_number,
                note=item.note,
                address=item.address
            )
            for item in data.orders
        ]
        logging.info(f"價格生成完成，訂單：{updated_orders}")
        return BrunchOrder(orders=updated_orders)

    def _increment_meals_number(self, data: BrunchOrder) -> BrunchOrder:
        """安全遞增取餐號碼"""
        current_number = POS_Robot.meals_counter
        updated_orders = [
            OrderItem(
                item=item.item,
                quantity=item.quantity,
                price=item.price,
                money=item.money,
                dining_option=item.dining_option,
                meals_number=current_number,
                note=item.note,
                address=item.address
            )
            for item in data.orders
        ]
        POS_Robot.meals_counter += 1
        logging.info(f"取餐號碼設置完成，號碼：{current_number}")
        return BrunchOrder(orders=updated_orders)

    # ---------------------------- 輸出方法 ----------------------------
    # 修改標準格式輸出為異步
    async def save_to_standard_formats(self, data: BrunchOrder):
        """異步標準格式輸出"""
        await asyncio.gather(
            self.save_to_json(data),
            self.save_to_custom_json(data),
            self.save_to_csv(data)
        )

    #修改後的異步存檔方法
    async def save_to_json(self, data: BrunchOrder):
        """JSON格式異步輸出"""
        try:
            async with aiofiles.open(
                f"order_data_{data.orders[0].meals_number}.json", 
                "w", 
                encoding='utf-8-sig'
            ) as f:
                json_str = json.dumps(
                    data.model_dump(by_alias=True, exclude_none=True),
                    ensure_ascii=False,
                    indent=2
                )
                await f.write(json_str)
            logging.info("標準JSON異步存檔完成")
        except Exception as e:
            logging.error(f"JSON異步存檔異常：{str(e)}")
            raise       

    async def save_to_custom_json(self, data: BrunchOrder):
            """自訂報表格式，直接在 Python 中生成，排除 null 值"""
            try:
                if not data.orders:
                    raise ValueError("訂單列表為空，無法生成報表")
                
                # 直接構建 CustomReport 物件
                custom_report = CustomReport(
                    items=[
                        {str(i+1): f"{item.item} x{item.quantity} {item.note or ''} ${item.money}"}
                        for i, item in enumerate(data.orders)
                    ],
                    total_money=sum(item.money for item in data.orders),
                    pickup_info=CustomReportPickupInfo(
                        method=data.orders[0].dining_option,
                        number=data.orders[0].meals_number,
                        address=data.orders[0].address or ""
                    )
                )
                formatted_data = custom_report.model_dump(by_alias=True, exclude_none=True)

                async with aiofiles.open(
                    f"order_custom_{data.orders[0].meals_number}.json",
                    "w",
                    encoding='utf-8-sig'
                ) as f:
                    await f.write(json.dumps(
                        formatted_data,
                        ensure_ascii=False,
                        indent=2
                    ))
                logging.info("自訂JSON異步存檔完成")
            except Exception as e:
                logging.error(f"自訂JSON存檔異常：{str(e)}")
                raise
            
    async def save_to_csv(self, data: BrunchOrder):
        """CSV格式異步輸出"""
        try:
            async with aiofiles.open(
                f"order_data_{data.orders[0].meals_number}.csv",
                "w",
                encoding='utf-8-sig',
                newline=''
            ) as f:
                writer = AsyncWriter(f)
                await writer.writerow(FIELD_LABELS)
                for item in data.orders:
                    row = [str(getattr(item, key) or "") for key in FIELD_KEYS]
                    await writer.writerow(row)
            logging.info("CSV異步存檔完成")
        except Exception as e:
            logging.error(f"CSV異步存檔異常：{str(e)}")
            raise

    def visualize_chain(self):
        """視覺化完整的 POS 處理鏈（full_chain + processing_chain）"""
        try:
            if hasattr(self.whole_chain, "get_graph"):
                graph = self.whole_chain.get_graph()
                print("【完整 POS 處理流程圖】")
                graph.print_ascii()
                # Note: to_json is not supported in recent LangChain versions
                # Save graph structure as DOT file instead (if needed)
                logging.info("流程圖已生成（ASCII格式）")
        except Exception as e:
            logging.warning(f"流程圖生成失敗：{str(e)}")

# ---------------------------- 主程式 ----------------------------
async def main():
    robot = POS_Robot()
    tasks = [
        robot.process_order("我要兩份火腿蛋吐司內用，兩杯冰紅茶外帶，紅茶要微糖去冰。還有一份鮪魚三明治外送到台北市信義區松壽路10號，加起司。"),
        robot.process_order("我要內用兩份火腿蛋吐司切邊，1杯中冰紅茶微糖去冰，1份鮪魚三明治加起司，1份火腿蛋吐司"),
        robot.process_order("我要兩份火腿蛋吐司內用，兩杯冰紅茶，紅茶要微糖去冰。")
    ]
    await asyncio.gather(*tasks)

if __name__ == '__main__':
    try:
        if sys.platform.startswith('win'):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("使用者中止操作")
    except Exception as e:
        logging.critical(f"系統嚴重錯誤：{str(e)}", exc_info=True)
