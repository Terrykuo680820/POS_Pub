import streamlit as st
import asyncio
from POS import POS_Robot  # 假設你的主程式檔名為 paste.py，且與本檔同資料夾

# 初始化 POS 機器人
@st.cache_resource
def get_robot():
    return POS_Robot()

robot = get_robot()

st.title("互動式點餐系統（Streamlit 版）")
user_input = st.text_area("請輸入您的訂單內容：", height=120, placeholder="例如：我要兩份火腿蛋吐司內用，兩杯冰紅茶外帶...")

if st.button("送出訂單"):
    with st.spinner("處理中，請稍候..."):
        # Streamlit 目前支援 async，可直接用 asyncio.run
        try:
            result = asyncio.run(robot.process_order(user_input))
            st.success("訂單處理完成！")
            # 顯示訂單明細
            for idx, order in enumerate(result.orders, 1):
                st.markdown(f"**{idx}. {order.item} x{order.quantity}**")
                st.write(f"單價：{order.price} 元，金額：{order.money} 元")
                st.write(f"取餐方式：{order.dining_option}，取餐號碼：{order.meals_number}")
                if order.note:
                    st.write(f"備註：{order.note}")
                if order.address:
                    st.write(f"外送地址：{order.address}")
                st.write("---")
            st.markdown(f"### 總金額：{sum(o.money for o in result.orders)} 元")
        except Exception as e:
            st.error(f"訂單處理失敗：{e}")
