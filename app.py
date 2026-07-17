# fitness-app/app.py
import os
import sqlite3
import base64
from datetime import datetime, date, timedelta
from io import BytesIO
from PIL import Image
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import hashlib
import dashscope
from http import HTTPStatus

# ------------------------------------------------------------------
# API Key 持久化（自动读写本地文件）
# ------------------------------------------------------------------
API_KEY_FILE = "api_key.txt"

def load_api_key():
    if os.path.exists(API_KEY_FILE):
        with open(API_KEY_FILE, "r") as f:
            return f.read().strip()
    return ""

def save_api_key(key):
    with open(API_KEY_FILE, "w") as f:
        f.write(key)

# ------------------------------------------------------------------
# 数据库初始化（自动创建表）
# ------------------------------------------------------------------
DB_PATH = "health.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS user (
        id INTEGER PRIMARY KEY,
        height_cm REAL,
        weight_kg REAL,
        age INTEGER,
        gender TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS daily_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT UNIQUE,
        weight_kg REAL,
        fatigue_score INTEGER,
        notes TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS food_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        meal_type TEXT,
        food_description TEXT,
        calories REAL,
        carbs REAL,
        protein REAL,
        fat REAL,
        fiber REAL,
        image_path TEXT,
        is_cheat_meal INTEGER DEFAULT 0,
        cheat_calories REAL,
        created_at TEXT DEFAULT (datetime('now'))
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS exercise_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        exercise_type TEXT,
        duration_min INTEGER,
        intensity INTEGER,
        notes TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS chat_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        role TEXT,
        content TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )''')
    conn.commit()
    conn.close()

init_db()

# ------------------------------------------------------------------
# 辅助函数
# ------------------------------------------------------------------
def get_db():
    return sqlite3.connect(DB_PATH)

def smooth_trend(data, window=7):
    if len(data) < window:
        return data
    return pd.Series(data).rolling(window=window, min_periods=1).mean().tolist()

def calculate_bmr(weight_kg, height_cm, age, gender):
    if gender == 'male':
        return 10 * weight_kg + 6.25 * height_cm - 5 * age + 5
    else:
        return 10 * weight_kg + 6.25 * height_cm - 5 * age - 161

def get_today_str():
    return date.today().isoformat()

def get_daily_summary(date_str):
    conn = get_db()
    df_food = pd.read_sql_query(f"SELECT SUM(calories) as total_cal FROM food_logs WHERE date='{date_str}'", conn)
    df_exercise = pd.read_sql_query(f"SELECT intensity, SUM(duration_min) as total_min FROM exercise_logs WHERE date='{date_str}' GROUP BY intensity", conn)
    conn.close()
    total_cal = df_food['total_cal'].iloc[0] if not df_food.empty and df_food['total_cal'].iloc[0] else 0
    exercise_burn = 0
    if not df_exercise.empty:
        for _, row in df_exercise.iterrows():
            intensity = row['intensity']
            mins = row['total_min']
            if intensity <= 2:
                exercise_burn += mins * 4
            elif intensity <= 4:
                exercise_burn += mins * 7
            else:
                exercise_burn += mins * 10
    return total_cal, exercise_burn

# ------------------------------------------------------------------
# 拳数换算（用于显示饮食建议）
# ------------------------------------------------------------------
def grams_to_fists(grams, food_type="carbs"):
    """
    根据食物类型换算成“拳”数
    碳水: 1拳 ≈ 50g 碳水（约200g米饭）
    蛋白质: 1拳 ≈ 25g 蛋白质（约150g瘦肉）
    纤维素: 1拳 ≈ 4g 纤维素（约100g蔬菜）
    """
    mapping = {
        "carbs": 50,   # 每拳碳水克数
        "protein": 25,
        "fiber": 4     # 每拳纤维素克数
    }
    per_fist = mapping.get(food_type, 50)
    fists = grams / per_fist
    fists_rounded = round(fists, 1)
    return f"{fists_rounded}拳"

# ------------------------------------------------------------------
# 通义千问 API 调用
# ------------------------------------------------------------------
def call_llm(messages, model="qwen-max", max_tokens=1024):
    try:
        dash_messages = [{"role": m["role"], "content": m["content"]} for m in messages]
        response = dashscope.Generation.call(
            model=model,
            messages=dash_messages,
            result_format='message',
            max_tokens=max_tokens,
            temperature=0.3
        )
        if response.status_code == HTTPStatus.OK:
            return response.output.choices[0].message.content
        else:
            return f"❌ 通义千问调用失败: {response.message}"
    except Exception as e:
        return f"❌ API调用出错: {str(e)}"

def analyze_food_text(description, weight_kg, height_cm, age, gender):
    prompt = f"""你是一个营养学专家。请根据下面描述的食物，估算其营养成分（以克为单位）。只返回 JSON 格式，不要额外解释。
示例输出：{{"food": "米饭", "calories": 200, "carbs": 40, "protein": 5, "fat": 1, "fiber": 0.5}}
用户描述：{description}"""
    messages = [{"role": "user", "content": prompt}]
    return call_llm(messages)

def analyze_food_image(image_bytes, weight_kg, height_cm, age, gender):
    base64_img = base64.b64encode(image_bytes).decode('utf-8')
    prompt = f"""请分析这张餐盘照片，估算每种食物的大概体积（用拳/克），并计算总卡路里、碳水、蛋白质、脂肪、纤维素克重。直接返回 JSON，如：{{"foods": ["米饭: 1拳", "西兰花: 半拳"], "total_calories": 300, "carbs": 45, "protein": 12, "fat": 8, "fiber": 3}}"""
    messages = [
        {"role": "user", "content": [
            {"text": prompt},
            {"image": f"data:image/jpeg;base64,{base64_img}"}
        ]}
    ]
    try:
        response = dashscope.MultiModelConversation.call(
            model='qwen-vl-plus',
            messages=messages
        )
        if response.status_code == HTTPStatus.OK:
            return response.output.choices[0].message.content[0]["text"]
        else:
            return f"❌ 视觉分析失败：{response.message}"
    except Exception as e:
        return f"❌ 视觉分析出错: {str(e)}"

# ------------------------------------------------------------------
# 运动推荐引擎
# ------------------------------------------------------------------
def recommend_exercise(date_str, fatigue_score):
    conn = get_db()
    today_exercises = pd.read_sql_query(f"SELECT * FROM exercise_logs WHERE date='{date_str}'", conn)
    conn.close()
    if fatigue_score >= 4:
        return {
            "type": "rest",
            "message": "⚠️ 高疲劳状态：建议动态拉伸、筋膜放松、散步或直接全天休息。持续高疲劳会导致皮质醇上升，阻碍减脂。"
        }
    muscle_map = {
        "背部": "胸部",
        "胸部": "背部",
        "腿部": "手臂",
        "手臂": "腿部",
        "肩膀": "背部",
        "腹部": "背部",
        "有氧": "力量训练"
    }
    if not today_exercises.empty:
        last_type = today_exercises.iloc[-1]['exercise_type']
        recommended = muscle_map.get(last_type, "全身训练")
        return {"type": recommended, "message": f"📋 昨日主要锻炼：{last_type}，建议今日：{recommended}"}
    else:
        return {"type": "全身训练", "message": "📋 建议进行常规全身训练或低强度有氧。"}

# ------------------------------------------------------------------
# 激励引擎
# ------------------------------------------------------------------
def generate_motivation(date_str, fatigue_score, weight_trend):
    conn = get_db()
    cursor = conn.execute("SELECT date FROM daily_logs ORDER BY date DESC LIMIT 30")
    rows = cursor.fetchall()
    conn.close()
    streak = 0
    if rows:
        today_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        for i, row in enumerate(rows):
            d = datetime.strptime(row[0], "%Y-%m-%d").date()
            if d == today_date - timedelta(days=i):
                streak += 1
            else:
                break
    if fatigue_score >= 4:
        return "🛌 今天休息也是变强的一部分，去睡觉吧！"
    if streak >= 7:
        return f"🔥 连续打卡 {streak} 天，你的坚持已经超出常人了！"
    if weight_trend == "down":
        return "📉 体重趋势下降，保持这个节奏！"
    if weight_trend == "up":
        return "📈 波动正常，别担心，明天继续调整。"
    return "💪 每一天都在变得更好，哪怕只是一点点。"

# ------------------------------------------------------------------
# Streamlit UI
# ------------------------------------------------------------------
st.set_page_config(page_title="健康管理 · 数据导向", layout="wide")
st.markdown("""
<style>
    .reportview-container { background: #f5f5f5; }
    .stButton>button { border-radius: 0; border: 1px solid #ccc; background: white; }
    .stTextInput>input, .stNumberInput>input { border-radius: 0; border: 1px solid #ccc; }
    .stDataFrame { border: none; }
</style>
""", unsafe_allow_html=True)

# 侧边栏导航
with st.sidebar:
    st.title("个人健康管理")
    page = st.radio("导航", ["📊 数据看板", "🍽️ 营养记录", "🏋️ 运动记录", "🤖 AI 教练", "⚙️ 设置"])

    # API Key 持久化（自动加载本地文件，用户修改后自动保存）
    saved_key = load_api_key()
    if "dashscope_key" not in st.session_state:
        st.session_state.dashscope_key = saved_key
    api_key = st.text_input("通义千问 API Key", value=st.session_state.dashscope_key, type="password")
    if api_key:
        st.session_state.dashscope_key = api_key
        dashscope.api_key = api_key
        save_api_key(api_key)  # 保存到本地文件
    else:
        st.warning("未设置 API Key，通义千问功能将不可用。")

# 主体内容
if page == "📊 数据看板":
    st.header("📊 数据看板")
    conn = get_db()
    df = pd.read_sql_query("SELECT date, weight_kg, fatigue_score FROM daily_logs ORDER BY date", conn)
    conn.close()

    if df.empty:
        st.info("还没有任何数据，请先在左侧菜单输入。")
    else:
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date')
        weight_smooth = smooth_trend(df['weight_kg'].tolist(), 7)
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df['date'], y=df['weight_kg'], mode='lines+markers', name='体重 (kg)'))
        fig.add_trace(go.Scatter(x=df['date'], y=weight_smooth, mode='lines', name='平滑趋势', line=dict(dash='dash', color='gray')))
        fig.add_trace(go.Bar(x=df['date'], y=df['fatigue_score'], name='疲劳度 (1-5)', yaxis='y2', marker_color='rgba(255, 100, 100, 0.5)'))
        fig.update_layout(
            yaxis=dict(title='体重 (kg)', range=[min(df['weight_kg'])-2, max(df['weight_kg'])+2]),
            yaxis2=dict(title='疲劳度', overlaying='y', side='right', range=[0, 6]),
            showlegend=True,
            hovermode='x unified',
            template='plotly_white'
        )
        st.plotly_chart(fig, use_container_width=True)

        st.subheader("今日快速记录")
        col1, col2, col3 = st.columns(3)
        with col1:
            today_weight = st.number_input("体重 (kg)", step=0.1, format="%.1f")
        with col2:
            today_fatigue = st.selectbox("主观疲劳度 (1-5)", [1,2,3,4,5], index=2)
        with col3:
            notes = st.text_area("备注（可选）", height=68, label_visibility="collapsed")

        if st.button("保存今日记录"):
            conn = get_db()
            try:
                conn.execute("INSERT OR REPLACE INTO daily_logs (date, weight_kg, fatigue_score, notes) VALUES (?, ?, ?, ?)",
                             (get_today_str(), today_weight, today_fatigue, notes))
                conn.commit()
                st.success("保存成功！")
            except Exception as e:
                st.error(f"保存失败: {e}")
            finally:
                conn.close()

        trend = "stable"
        if len(df) >= 2:
            if df['weight_kg'].iloc[-1] < df['weight_kg'].iloc[-2]:
                trend = "down"
            elif df['weight_kg'].iloc[-1] > df['weight_kg'].iloc[-2]:
                trend = "up"
        last_fatigue = df['fatigue_score'].iloc[-1] if not df.empty else 3
        motivation = generate_motivation(get_today_str(), last_fatigue, trend)
        st.info(motivation)

elif page == "🍽️ 营养记录":
    st.header("🍽️ 营养记录")
    conn = get_db()
    user = conn.execute("SELECT * FROM user ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    if not user:
        st.warning("请先在 ⚙️ 设置 中输入身体数据。")
        st.stop()
    height_cm, weight_kg, age, gender = user[1], user[2], user[3], user[4]
    bmr = calculate_bmr(weight_kg, height_cm, age, gender)
    st.markdown(f"**基础代谢 (BMR): {bmr:.0f} kcal**")

    method = st.radio("记录方式", ["📝 文字描述", "📷 拍照/上传", "🍔 Cheat Meal"])

    if method == "📝 文字描述":
        text = st.text_area("例如：1.2拳米饭，1拳油菜")
        if st.button("分析文字"):
            if not st.session_state.dashscope_key:
                st.error("请先在侧边栏设置通义千问 API Key")
            else:
                result = analyze_food_text(text, weight_kg, height_cm, age, gender)
                st.json(result)
                import json
                try:
                    data = json.loads(result)
                    conn = get_db()
                    conn.execute("INSERT INTO food_logs (date, meal_type, food_description, calories, carbs, protein, fat, fiber) VALUES (?,?,?,?,?,?,?,?)",
                                 (get_today_str(), "text_input", text, data.get('calories',0), data.get('carbs',0), data.get('protein',0), data.get('fat',0), data.get('fiber',0)))
                    conn.commit()
                    conn.close()
                    st.success("已保存到数据库")
                except:
                    st.warning("未能自动保存，请手动输入（见下方快速记录）")

    elif method == "📷 拍照/上传":
        uploaded = st.file_uploader("上传餐盘照片", type=['jpg','jpeg','png'])
        if uploaded and st.button("分析照片"):
            if not st.session_state.dashscope_key:
                st.error("请先设置通义千问 API Key")
            else:
                img_bytes = uploaded.read()
                result = analyze_food_image(img_bytes, weight_kg, height_cm, age, gender)
                st.json(result)
                import json
                try:
                    data = json.loads(result)
                    conn = get_db()
                    conn.execute("INSERT INTO food_logs (date, meal_type, food_description, calories, carbs, protein, fat, fiber, image_path) VALUES (?,?,?,?,?,?,?,?,?)",
                                 (get_today_str(), "vision", result, data.get('total_calories',0), data.get('carbs',0), data.get('protein',0), data.get('fat',0), data.get('fiber',0), "uploaded_image"))
                    conn.commit()
                    conn.close()
                    st.success("已保存")
                except:
                    st.warning("解析JSON失败，请手动输入")

    else:  # Cheat Meal
        cheat_cal = st.number_input("估算总卡路里", step=50, value=500)
        if st.button("记录 Cheat Meal"):
            conn = get_db()
            conn.execute("INSERT INTO food_logs (date, meal_type, food_description, calories, is_cheat_meal, cheat_calories) VALUES (?,?,?,?,?,?)",
                         (get_today_str(), "cheat", "Cheat Meal", cheat_cal, 1, cheat_cal))
            conn.commit()
            conn.close()
            st.success("已记录放纵餐")

    st.subheader("今日饮食汇总")
    conn = get_db()
    today_food = pd.read_sql_query(f"SELECT * FROM food_logs WHERE date='{get_today_str()}'", conn)
    conn.close()
    if not today_food.empty:
        total_cal = today_food['calories'].sum()
        st.write(f"总摄入：{total_cal:.0f} kcal")
        st.dataframe(today_food[['meal_type','food_description','calories','carbs','protein','fat','fiber']], use_container_width=True)
    else:
        st.info("今天还没有记录")

    # --- 明日饮食建议（用拳表示）---
    st.subheader("🍽️ 明日饮食建议")
    _, exercise_burn = get_daily_summary(get_today_str())
    target_cal = bmr + exercise_burn * 0.5
    recommended_protein = weight_kg * 2          # g
    recommended_fat = weight_kg * 0.8            # g
    recommended_carbs = weight_kg * 1            # g
    recommended_fiber = weight_kg * 0.04         # g

    # 换算成拳
    carbs_fists = grams_to_fists(recommended_carbs, "carbs")
    protein_fists = grams_to_fists(recommended_protein, "protein")
    fiber_fists = grams_to_fists(recommended_fiber, "fiber")

    st.markdown(f"**目标总摄入**: {target_cal:.0f} kcal")
    st.markdown(f"- 🍚 碳水: {carbs_fists}（{recommended_carbs:.0f}g）")
    st.markdown(f"- 🥩 蛋白质: {protein_fists}（{recommended_protein:.0f}g）")
    st.markdown(f"- 🥬 纤维素: {fiber_fists}（{recommended_fiber:.0f}g）")
    st.markdown(f"- 🧈 脂肪: {recommended_fat:.0f}g (≈{recommended_fat*9:.0f} kcal)")

elif page == "🏋️ 运动记录":
    st.header("🏋️ 运动记录")
    with st.form("exercise_form"):
        col1, col2, col3 = st.columns(3)
        with col1:
            ex_type = st.text_input("运动类型（如：背部训练、跑步）", value="")
        with col2:
            duration = st.number_input("时长 (分钟)", step=5, value=30)
        with col3:
            intensity = st.selectbox("主观疲劳度 (1-5)", [1,2,3,4,5], index=2)
        notes = st.text_area("备注", height=68)
        submitted = st.form_submit_button("保存运动记录")
        if submitted:
            if not ex_type:
                st.error("请输入运动类型")
            else:
                conn = get_db()
                conn.execute("INSERT INTO exercise_logs (date, exercise_type, duration_min, intensity, notes) VALUES (?,?,?,?,?)",
                             (get_today_str(), ex_type, duration, intensity, notes))
                conn.commit()
                conn.close()
                st.success("运动记录已保存")

    conn = get_db()
    today_ex = pd.read_sql_query(f"SELECT * FROM exercise_logs WHERE date='{get_today_str()}'", conn)
    conn.close()
    st.subheader("今日运动")
    if not today_ex.empty:
        st.dataframe(today_ex[['exercise_type','duration_min','intensity','notes']], use_container_width=True)
    else:
        st.info("还没有记录")

    conn = get_db()
    last_fatigue = conn.execute(f"SELECT fatigue_score FROM daily_logs WHERE date='{get_today_str()}'").fetchone()
    fatigue = last_fatigue[0] if last_fatigue else 3
    conn.close()
    rec = recommend_exercise(get_today_str(), fatigue)
    st.subheader("📋 明日运动推荐")
    st.info(rec['message'])

    with st.expander("查看历史运动记录"):
        conn = get_db()
        df_ex_all = pd.read_sql_query("SELECT * FROM exercise_logs ORDER BY date DESC LIMIT 50", conn)
        conn.close()
        st.dataframe(df_ex_all, use_container_width=True)

elif page == "🤖 AI 教练":
    st.header("🤖 AI 教练")
    conn = get_db()
    today_str = get_today_str()
    daily = conn.execute(f"SELECT weight_kg, fatigue_score FROM daily_logs WHERE date='{today_str}'").fetchone()
    weight = daily[0] if daily else "未记录"
    fatigue = daily[1] if daily else "未记录"
    food_today = pd.read_sql_query(f"SELECT SUM(calories) as cal FROM food_logs WHERE date='{today_str}'", conn)
    total_cal = food_today.iloc[0]['cal'] if not food_today.empty and food_today.iloc[0]['cal'] else 0
    ex_today = pd.read_sql_query(f"SELECT exercise_type, duration_min, intensity FROM exercise_logs WHERE date='{today_str}'", conn)
    ex_summary = ex_today.to_string() if not ex_today.empty else "无"
    conn.close()

    context = f"今日日期：{today_str}\n体重：{weight}kg\n疲劳度：{fatigue}/5\n饮食总摄入：{total_cal}kcal\n运动记录：\n{ex_summary}"

    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    for msg in st.session_state.chat_history[-10:]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if prompt := st.chat_input("问你的运动/健康问题..."):
        st.session_state.chat_history.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        if not st.session_state.dashscope_key:
            st.error("请先在侧边栏设置通义千问 API Key")
        else:
            messages = [
                {"role": "system", "content": f"你是一个专业的健身教练与营养顾问。以下是用户的今日上下文：\n{context}\n请根据这些信息回答用户的问题，保持简洁、数据导向。"},
            ]
            for msg in st.session_state.chat_history[-6:]:
                messages.append({"role": msg["role"], "content": msg["content"]})
            response = call_llm(messages)
            st.session_state.chat_history.append({"role": "assistant", "content": response})
            with st.chat_message("assistant"):
                st.markdown(response)

elif page == "⚙️ 设置":
    st.header("⚙️ 设置")
    with st.form("user_settings"):
        col1, col2 = st.columns(2)
        with col1:
            height = st.number_input("身高 (cm)", step=1, value=170)
            weight_input = st.number_input("初始体重 (kg)", step=0.1, value=70.0)
        with col2:
            age = st.number_input("年龄", step=1, value=30)
            gender = st.selectbox("性别", ["male", "female"])
        if st.form_submit_button("保存设置"):
            conn = get_db()
            existing = conn.execute("SELECT id FROM user ORDER BY id DESC LIMIT 1").fetchone()
            if existing:
                conn.execute("UPDATE user SET height_cm=?, weight_kg=?, age=?, gender=? WHERE id=?", (height, weight_input, age, gender, existing[0]))
            else:
                conn.execute("INSERT INTO user (height_cm, weight_kg, age, gender) VALUES (?,?,?,?)", (height, weight_input, age, gender))
            conn.commit()
            conn.close()
            st.success("设置已保存")

    st.subheader("数据管理")
    if st.button("导出所有数据为 CSV"):
        conn = get_db()
        tables = ['daily_logs', 'food_logs', 'exercise_logs']
        for table in tables:
            df = pd.read_sql_query(f"SELECT * FROM {table}", conn)
            csv_data = df.to_csv(index=False)
            st.download_button(label=f"下载 {table}.csv", data=csv_data, file_name=f"{table}.csv", mime="text/csv")
        conn.close()
