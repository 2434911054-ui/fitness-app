# fitness-app/app.py
import os
import base64
from datetime import datetime, date, timedelta
import json
import sqlite3

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import dashscope
from http import HTTPStatus
import psycopg2
from psycopg2.extras import RealDictCursor

# 页面配置必须在所有操作之前
st.set_page_config(page_title="健康管理 · 数据导向", layout="centered")

# ------------------------------------------------------------------
# 数据库连接（优先使用 DB_URI，回退到本地 SQLite）
# ------------------------------------------------------------------
# 尝试从 Streamlit Secrets 获取，如果没有则尝试本地环境变量
try:
    DB_URI = st.secrets.get("DB_URI", os.getenv("DB_URI", ""))
except FileNotFoundError:
    DB_URI = os.getenv("DB_URI", "")

def get_db():
    """返回数据库连接对象（PostgreSQL 或 SQLite）"""
    if DB_URI:
        # PostgreSQL
        conn = psycopg2.connect(DB_URI, sslmode='require')
        return conn
    else:
        return sqlite3.connect("health.db")

def execute_sql(sql, params=None, fetch=False):
    """统一执行 SQL，返回结果（如果有）"""
    conn = get_db()
    try:
        if DB_URI:
            # PostgreSQL 使用 RealDictCursor
            cur = conn.cursor(cursor_factory=RealDictCursor)
        else:
            cur = conn.cursor()
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        
        if fetch:
            result = cur.fetchall()
        else:
            result = None
        conn.commit()
        cur.close()
        return result
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

# ------------------------------------------------------------------
# 数据库初始化（自动创建表，加入缓存避免重复执行）
# ------------------------------------------------------------------
@st.cache_resource
def init_db_once():
    """创建所有必要表，兼容 PostgreSQL 和 SQLite"""
    if DB_URI:
        # PostgreSQL 建表语句
        statements = [
            """CREATE TABLE IF NOT EXISTS "user" (
                id SERIAL PRIMARY KEY,
                height_cm REAL,
                weight_kg REAL,
                age INTEGER,
                gender TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );""",
            """CREATE TABLE IF NOT EXISTS daily_logs (
                id SERIAL PRIMARY KEY,
                date DATE UNIQUE,
                weight_kg REAL,
                fatigue_score INTEGER,
                notes TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );""",
            """CREATE TABLE IF NOT EXISTS food_logs (
                id SERIAL PRIMARY KEY,
                date DATE,
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
                created_at TIMESTAMP DEFAULT NOW()
            );""",
            """CREATE TABLE IF NOT EXISTS exercise_logs (
                id SERIAL PRIMARY KEY,
                date DATE,
                exercise_type TEXT,
                duration_min INTEGER,
                intensity INTEGER,
                notes TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );""",
            """CREATE TABLE IF NOT EXISTS chat_history (
                id SERIAL PRIMARY KEY,
                date DATE,
                role TEXT,
                content TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );"""
        ]
        for stmt in statements:
            execute_sql(stmt)
    else:
        # SQLite 回退（本地使用）
        conn = sqlite3.connect("health.db")
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS "user" (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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

# 触发数据库初始化
init_db_once()

# ------------------------------------------------------------------
# API Key 本地持久化（可选）
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
# 辅助函数
# ------------------------------------------------------------------
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
    """返回当天饮食总卡、运动消耗"""
    try:
        if DB_URI:
            rows = execute_sql("SELECT SUM(calories) as total_cal FROM food_logs WHERE date = %s", (date_str,), fetch=True)
            total_cal = rows[0]['total_cal'] if rows and rows[0]['total_cal'] else 0
        else:
            conn = get_db()
            df_food = pd.read_sql_query(f"SELECT SUM(calories) as total_cal FROM food_logs WHERE date='{date_str}'", conn)
            total_cal = df_food['total_cal'].iloc[0] if not df_food.empty and df_food['total_cal'].iloc[0] else 0
            conn.close()
    except:
        total_cal = 0

    try:
        if DB_URI:
            rows = execute_sql("SELECT intensity, SUM(duration_min) as total_min FROM exercise_logs WHERE date = %s GROUP BY intensity", (date_str,), fetch=True)
        else:
            conn = get_db()
            cur = conn.execute(f"SELECT intensity, SUM(duration_min) as total_min FROM exercise_logs WHERE date='{date_str}' GROUP BY intensity")
            rows = cur.fetchall()
            conn.close()
        
        exercise_burn = 0
        for row in rows:
            intensity = row['intensity'] if isinstance(row, dict) else row[0]
            mins = row['total_min'] if isinstance(row, dict) else row[1]
            if intensity <= 2:
                exercise_burn += mins * 4
            elif intensity <= 4:
                exercise_burn += mins * 7
            else:
                exercise_burn += mins * 10
    except:
        exercise_burn = 0
    return total_cal, exercise_burn

def grams_to_fists(grams, food_type="carbs"):
    mapping = {"carbs": 50, "protein": 25, "fiber": 4}
    per_fist = mapping.get(food_type, 50)
    fists = grams / per_fist
    return f"{round(fists,1)}拳"

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
        response = dashscope.MultiModelConversation.call(model='qwen-vl-plus', messages=messages)
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
    try:
        if DB_URI:
            rows = execute_sql("SELECT * FROM exercise_logs WHERE date = %s", (date_str,), fetch=True)
        else:
            conn = get_db()
            cur = conn.execute(f"SELECT * FROM exercise_logs WHERE date='{date_str}'")
            rows = cur.fetchall()
            conn.close()
    except:
        rows = []
    
    if fatigue_score >= 4:
        return {"type": "rest", "message": "⚠️ 高疲劳状态：建议动态拉伸、筋膜放松、散步或直接全天休息。持续高疲劳会导致皮质醇上升，阻碍减脂。"}
    
    muscle_map = {"背部": "胸部","胸部": "背部","腿部": "手臂","手臂": "腿部","肩膀": "背部","腹部": "背部","有氧": "力量训练"}
    if rows:
        last_type = rows[-1]['exercise_type'] if isinstance(rows[-1], dict) else rows[-1][2]
        recommended = muscle_map.get(last_type, "全身训练")
        return {"type": recommended, "message": f"📋 昨日主要锻炼：{last_type}，建议今日：{recommended}"}
    else:
        return {"type": "全身训练", "message": "📋 建议进行常规全身训练或低强度有氧。"}

# ------------------------------------------------------------------
# 激励引擎
# ------------------------------------------------------------------
def generate_motivation(date_str, fatigue_score, weight_trend):
    try:
        if DB_URI:
            rows = execute_sql("SELECT date FROM daily_logs ORDER BY date DESC LIMIT 30", fetch=True)
            dates = [row['date'] for row in rows]
        else:
            conn = get_db()
            cursor = conn.execute("SELECT date FROM daily_logs ORDER BY date DESC LIMIT 30")
            dates = [row[0] for row in cursor.fetchall()]
            conn.close()
    except:
        dates = []
    
    streak = 0
    if dates:
        today_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        for i, d in enumerate(dates):
            if isinstance(d, str):
                d = datetime.strptime(d, "%Y-%m-%d").date()
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
# Streamlit UI & 布局样式
# ------------------------------------------------------------------
st.markdown("""
<style>
    .reportview-container { background: #f5f5f5; }
    .stButton>button { border-radius: 8px; border: 1px solid #ccc; background: white; width: 100%; }
    .stTextInput>input, .stNumberInput>input { border-radius: 8px; border: 1px solid #ccc; }
    .stDataFrame { border: none; }
</style>
""", unsafe_allow_html=True)

# 处理 API Key
env_key = os.getenv("DASHSCOPE_API_KEY", "")
saved_key = load_api_key()
default_key = env_key if env_key else saved_key
if "dashscope_key" not in st.session_state:
    st.session_state.dashscope_key = default_key

# 设置全局 dashscope key
if st.session_state.dashscope_key:
    dashscope.api_key = st.session_state.dashscope_key

st.title("🏃‍♂️ 个人健康管理")

# 移动端友好的顶部标签页导航
tab_kanban, tab_food, tab_exercise, tab_ai, tab_settings = st.tabs(["📊 看板", "🍽️ 饮食", "🏋️ 运动", "🤖 AI", "⚙️ 设置"])

# =============================== 看板页 ===============================
with tab_kanban:
    try:
        if DB_URI:
            rows = execute_sql("SELECT date, weight_kg, fatigue_score FROM daily_logs ORDER BY date", fetch=True)
            df = pd.DataFrame(rows) if rows else pd.DataFrame()
        else:
            conn = get_db()
            df = pd.read_sql_query("SELECT date, weight_kg, fatigue_score FROM daily_logs ORDER BY date", conn)
            conn.close()
    except:
        df = pd.DataFrame()

    if df.empty:
        st.info("还没有任何数据，请先在下方输入。")
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
            template='plotly_white',
            margin=dict(l=0, r=0, t=30, b=0) # 适配手机屏幕
        )
        st.plotly_chart(fig, use_container_width=True)

        trend = "stable"
        if len(df) >= 2:
            if df['weight_kg'].iloc[-1] < df['weight_kg'].iloc[-2]:
                trend = "down"
            elif df['weight_kg'].iloc[-1] > df['weight_kg'].iloc[-2]:
                trend = "up"
        last_fatigue = df['fatigue_score'].iloc[-1] if not df.empty else 3
        motivation = generate_motivation(get_today_str(), last_fatigue, trend)
        st.info(motivation)

    st.subheader("今日快速打卡")
    col1, col2 = st.columns(2)
    with col1:
        today_weight = st.number_input("体重 (kg)", step=0.1, format="%.1f")
    with col2:
        today_fatigue = st.selectbox("主观疲劳度 (1-5)", [1,2,3,4,5], index=2)
    
    notes = st.text_area("备注（可选）", height=68, label_visibility="collapsed")

    if st.button("保存今日打卡"):
        try:
            if DB_URI:
                execute_sql(
                    "INSERT INTO daily_logs (date, weight_kg, fatigue_score, notes) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (date) DO UPDATE SET weight_kg=EXCLUDED.weight_kg, fatigue_score=EXCLUDED.fatigue_score, notes=EXCLUDED.notes",
                    (get_today_str(), today_weight, today_fatigue, notes)
                )
            else:
                conn = get_db()
                conn.execute("INSERT OR REPLACE INTO daily_logs (date, weight_kg, fatigue_score, notes) VALUES (?, ?, ?, ?)",
                             (get_today_str(), today_weight, today_fatigue, notes))
                conn.commit()
                conn.close()
            st.success("打卡成功！")
        except Exception as e:
            st.error(f"保存失败: {e}")

# =============================== 饮食页 ===============================
with tab_food:
    try:
        if DB_URI:
            rows = execute_sql("SELECT * FROM \"user\" ORDER BY id DESC LIMIT 1", fetch=True)
            user = rows[0] if rows else None
        else:
            conn = get_db()
            user = conn.execute("SELECT * FROM \"user\" ORDER BY id DESC LIMIT 1").fetchone()
            conn.close()
    except:
        user = None

    if not user:
        st.warning("请先在 ⚙️ 设置 页签中保存您的基础身体数据。")
    else:
        # 从用户数据中提取信息（兼容dict和tuple）
        if isinstance(user, dict):
            height_cm = user['height_cm']
            weight_kg = user['weight_kg']
            age = user['age']
            gender = user['gender']
        else:
            height_cm = user[1]
            weight_kg = user[2]
            age = user[3]
            gender = user[4]

        bmr = calculate_bmr(weight_kg, height_cm, age, gender)
        st.markdown(f"**您的基础代谢 (BMR): {bmr:.0f} kcal**")

        method = st.radio("记录方式", ["📝 文字描述", "📷 拍照/上传", "🍔 Cheat Meal"], horizontal=True)

        if method == "📝 文字描述":
            text = st.text_area("例如：1.2拳米饭，1拳油菜", height=80)
            if st.button("AI 分析并记录"):
                if not st.session_state.dashscope_key:
                    st.error("请先在 ⚙️ 设置 页签设置通义千问 API Key")
                else:
                    with st.spinner("AI正在分析中..."):
                        result = analyze_food_text(text, weight_kg, height_cm, age, gender)
                        st.json(result)
                        try:
                            data = json.loads(result)
                            if DB_URI:
                                execute_sql(
                                    "INSERT INTO food_logs (date, meal_type, food_description, calories, carbs, protein, fat, fiber) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                                    (get_today_str(), "text_input", text, data.get('calories',0), data.get('carbs',0), data.get('protein',0), data.get('fat',0), data.get('fiber',0))
                                )
                            else:
                                conn = get_db()
                                conn.execute("INSERT INTO food_logs (date, meal_type, food_description, calories, carbs, protein, fat, fiber) VALUES (?,?,?,?,?,?,?,?)",
                                             (get_today_str(), "text_input", text, data.get('calories',0), data.get('carbs',0), data.get('protein',0), data.get('fat',0), data.get('fiber',0)))
                                conn.commit()
                                conn.close()
                            st.success("已成功写入数据库")
                        except:
                            st.warning("JSON解析异常，未能自动入库")

        elif method == "📷 拍照/上传":
            uploaded = st.file_uploader("上传餐盘照片", type=['jpg','jpeg','png'])
            if uploaded and st.button("AI 视觉分析"):
                if not st.session_state.dashscope_key:
                    st.error("请先在 ⚙️ 设置 页签设置通义千问 API Key")
                else:
                    with st.spinner("AI看图中..."):
                        img_bytes = uploaded.read()
                        result = analyze_food_image(img_bytes, weight_kg, height_cm, age, gender)
                        st.json(result)
                        try:
                            data = json.loads(result)
                            if DB_URI:
                                execute_sql(
                                    "INSERT INTO food_logs (date, meal_type, food_description, calories, carbs, protein, fat, fiber, image_path) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                                    (get_today_str(), "vision", result, data.get('total_calories',0), data.get('carbs',0), data.get('protein',0), data.get('fat',0), data.get('fiber',0), "uploaded_image")
                                )
                            else:
                                conn = get_db()
                                conn.execute("INSERT INTO food_logs (date, meal_type, food_description, calories, carbs, protein, fat, fiber, image_path) VALUES (?,?,?,?,?,?,?,?,?)",
                                             (get_today_str(), "vision", result, data.get('total_calories',0), data.get('carbs',0), data.get('protein',0), data.get('fat',0), data.get('fiber',0), "uploaded_image"))
                                conn.commit()
                                conn.close()
                            st.success("已成功写入数据库")
                        except:
                            st.warning("解析JSON失败，未能自动保存")

        else:  # Cheat Meal
            cheat_cal = st.number_input("估算总卡路里", step=50, value=500)
            if st.button("记录 Cheat Meal"):
                if DB_URI:
                    execute_sql(
                        "INSERT INTO food_logs (date, meal_type, food_description, calories, is_cheat_meal, cheat_calories) VALUES (%s,%s,%s,%s,%s,%s)",
                        (get_today_str(), "cheat", "Cheat Meal", cheat_cal, 1, cheat_cal)
                    )
                else:
                    conn = get_db()
                    conn.execute("INSERT INTO food_logs (date, meal_type, food_description, calories, is_cheat_meal, cheat_calories) VALUES (?,?,?,?,?,?)",
                                 (get_today_str(), "cheat", "Cheat Meal", cheat_cal, 1, cheat_cal))
                    conn.commit()
                    conn.close()
                st.success("放纵餐已记录，偶尔放松是允许的！")

        st.divider()
        st.subheader("今日饮食汇总")
        try:
            if DB_URI:
                rows = execute_sql("SELECT * FROM food_logs WHERE date = %s", (get_today_str(),), fetch=True)
                today_food = pd.DataFrame(rows) if rows else pd.DataFrame()
            else:
                conn = get_db()
                today_food = pd.read_sql_query(f"SELECT * FROM food_logs WHERE date='{get_today_str()}'", conn)
                conn.close()
        except:
            today_food = pd.DataFrame()

        if not today_food.empty:
            total_cal = today_food['calories'].sum() if 'calories' in today_food else 0
            st.metric("今日总摄入", f"{total_cal:.0f} kcal")
            cols = ['meal_type','food_description','calories','carbs','protein','fat','fiber']
            cols = [c for c in cols if c in today_food.columns]
            st.dataframe(today_food[cols], use_container_width=True)
        else:
            st.info("今天还没有记录饮食")

        # 明日饮食建议（用拳表示）
        st.subheader("💡 饮食结构建议")
        _, exercise_burn = get_daily_summary(get_today_str())
        target_cal = bmr + exercise_burn * 0.5
        recommended_protein = weight_kg * 2
        recommended_fat = weight_kg * 0.8
        recommended_carbs = weight_kg * 1
        recommended_fiber = weight_kg * 0.04

        carbs_fists = grams_to_fists(recommended_carbs, "carbs")
        protein_fists = grams_to_fists(recommended_protein, "protein")
        fiber_fists = grams_to_fists(recommended_fiber, "fiber")

        st.markdown(f"**目标总摄入**: {target_cal:.0f} kcal")
        st.markdown(f"- 🍚 **碳水**: {carbs_fists}（{recommended_carbs:.0f}g）")
        st.markdown(f"- 🥩 **蛋白质**: {protein_fists}（{recommended_protein:.0f}g）")
        st.markdown(f"- 🥬 **纤维素**: {fiber_fists}（{recommended_fiber:.0f}g）")

# =============================== 运动页 ===============================
with tab_exercise:
    ex_type = st.text_input("运动类型（如：背部、跑步）")
    col1, col2 = st.columns(2)
    with col1:
        duration = st.number_input("时长 (分钟)", step=5, value=30)
    with col2:
        intensity = st.selectbox("疲劳度 (1轻松-5力竭)", [1,2,3,4,5], index=2)
    
    ex_notes = st.text_area("运动感受或备注", height=68)
    
    if st.button("保存运动记录"):
        if not ex_type:
            st.error("请输入运动类型")
        else:
            try:
                if DB_URI:
                    execute_sql(
                        "INSERT INTO exercise_logs (date, exercise_type, duration_min, intensity, notes) VALUES (%s,%s,%s,%s,%s)",
                        (get_today_str(), ex_type, duration, intensity, ex_notes)
                    )
                else:
                    conn = get_db()
                    conn.execute("INSERT INTO exercise_logs (date, exercise_type, duration_min, intensity, notes) VALUES (?,?,?,?,?)",
                                 (get_today_str(), ex_type, duration, intensity, ex_notes))
                    conn.commit()
                    conn.close()
                st.success("运动记录已保存")
            except Exception as e:
                st.error(f"保存失败: {e}")

    st.divider()
    
    try:
        if DB_URI:
            rows = execute_sql("SELECT * FROM exercise_logs WHERE date = %s", (get_today_str(),), fetch=True)
            today_ex = pd.DataFrame(rows) if rows else pd.DataFrame()
        else:
            conn = get_db()
            today_ex = pd.read_sql_query(f"SELECT * FROM exercise_logs WHERE date='{get_today_str()}'", conn)
            conn.close()
    except:
        today_ex = pd.DataFrame()

    st.subheader("今日运动清单")
    if not today_ex.empty:
        cols = ['exercise_type','duration_min','intensity','notes']
        cols = [c for c in cols if c in today_ex.columns]
        st.dataframe(today_ex[cols], use_container_width=True)
    else:
        st.info("今天还没有流汗哦")

    # 疲劳度获取与推荐
    try:
        if DB_URI:
            rows = execute_sql("SELECT fatigue_score FROM daily_logs WHERE date = %s", (get_today_str(),), fetch=True)
            fatigue = rows[0]['fatigue_score'] if rows else 3
        else:
            conn = get_db()
            row = conn.execute(f"SELECT fatigue_score FROM daily_logs WHERE date='{get_today_str()}'").fetchone()
            fatigue = row[0] if row else 3
            conn.close()
    except:
        fatigue = 3

    rec = recommend_exercise(get_today_str(), fatigue)
    st.subheader("📋 明日安排建议")
    st.info(rec['message'])

# =============================== AI 教练页 ===============================
with tab_ai:
    today_str = get_today_str()
    # 取当日数据
    try:
        if DB_URI:
            rows = execute_sql("SELECT weight_kg, fatigue_score FROM daily_logs WHERE date = %s", (today_str,), fetch=True)
            daily_info = rows[0] if rows else None
            weight = daily_info['weight_kg'] if daily_info else "未记录"
            fatigue = daily_info['fatigue_score'] if daily_info else "未记录"
            rows_cal = execute_sql("SELECT SUM(calories) as cal FROM food_logs WHERE date = %s", (today_str,), fetch=True)
            total_cal = rows_cal[0]['cal'] if rows_cal and rows_cal[0]['cal'] else 0
            rows_ex = execute_sql("SELECT exercise_type, duration_min, intensity FROM exercise_logs WHERE date = %s", (today_str,), fetch=True)
            ex_summary = pd.DataFrame(rows_ex).to_string() if rows_ex else "无"
        else:
            conn = get_db()
            daily = conn.execute(f"SELECT weight_kg, fatigue_score FROM daily_logs WHERE date='{today_str}'").fetchone()
            weight = daily[0] if daily else "未记录"
            fatigue = daily[1] if daily else "未记录"
            food_today = pd.read_sql_query(f"SELECT SUM(calories) as cal FROM food_logs WHERE date='{today_str}'", conn)
            total_cal = food_today.iloc[0]['cal'] if not food_today.empty and food_today.iloc[0]['cal'] else 0
            ex_today = pd.read_sql_query(f"SELECT exercise_type, duration_min, intensity FROM exercise_logs WHERE date='{today_str}'", conn)
            ex_summary = ex_today.to_string() if not ex_today.empty else "无"
            conn.close()
    except:
        weight = "未记录"
        fatigue = "未记录"
        total_cal = 0
        ex_summary = "无"

    context = f"今日日期：{today_str}\n体重：{weight}kg\n疲劳度：{fatigue}/5\n饮食总摄入：{total_cal}kcal\n运动记录：\n{ex_summary}"

    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    # 渲染历史对话
    for msg in st.session_state.chat_history[-10:]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if prompt := st.chat_input("问教练关于饮食/训练的建议..."):
        st.session_state.chat_history.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
            
        if not st.session_state.dashscope_key:
            st.error("请先在 ⚙️ 设置 页签设置通义千问 API Key")
        else:
            messages = [
                {"role": "system", "content": f"你是一个专业的健身教练与营养顾问。以下是用户的今日上下文：\n{context}\n请根据这些信息回答用户的问题，保持简洁、数据导向。"},
            ]
            for msg in st.session_state.chat_history[-6:]:
                messages.append({"role": msg["role"], "content": msg["content"]})
            
            with st.chat_message("assistant"):
                with st.spinner("教练思考中..."):
                    response = call_llm(messages)
                    st.markdown(response)
                    st.session_state.chat_history.append({"role": "assistant", "content": response})

# =============================== 设置页 ===============================
with tab_settings:
    st.subheader("基础信息设置")
    with st.form("user_settings"):
        col1, col2 = st.columns(2)
        with col1:
            height = st.number_input("身高 (cm)", step=1, value=170)
            weight_input = st.number_input("初始体重 (kg)", step=0.1, value=70.0)
        with col2:
            age = st.number_input("年龄", step=1, value=30)
            gender = st.selectbox("性别", ["male", "female"])
        if st.form_submit_button("保存身体数据"):
            try:
                if DB_URI:
                    existing = execute_sql("SELECT id FROM \"user\" ORDER BY id DESC LIMIT 1", fetch=True)
                    if existing:
                        execute_sql("UPDATE \"user\" SET height_cm=%s, weight_kg=%s, age=%s, gender=%s WHERE id=%s",
                                     (height, weight_input, age, gender, existing[0]['id']))
                    else:
                        execute_sql("INSERT INTO \"user\" (height_cm, weight_kg, age, gender) VALUES (%s,%s,%s,%s)",
                                     (height, weight_input, age, gender))
                else:
                    conn = get_db()
                    existing = conn.execute("SELECT id FROM \"user\" ORDER BY id DESC LIMIT 1").fetchone()
                    if existing:
                        conn.execute("UPDATE \"user\" SET height_cm=?, weight_kg=?, age=?, gender=? WHERE id=?", (height, weight_input, age, gender, existing[0]))
                    else:
                        conn.execute("INSERT INTO \"user\" (height_cm, weight_kg, age, gender) VALUES (?,?,?,?)", (height, weight_input, age, gender))
                    conn.commit()
                    conn.close()
                st.success("数据已保存")
            except Exception as e:
                st.error(f"保存失败: {e}")

    st.divider()
    st.subheader("API 密钥配置")
    api_key_input = st.text_input("通义千问 API Key", value=st.session_state.dashscope_key, type="password")
    if st.button("保存 API Key"):
        if api_key_input:
            st.session_state.dashscope_key = api_key_input
            dashscope.api_key = api_key_input
            if not env_key:
                save_api_key(api_key_input)
            st.success("API Key 更新成功")
        else:
            st.warning("请输入有效的 Key")

    st.divider()
    st.subheader("数据导出")
    if st.button("导出所有云端数据"):
        try:
            tables = ['daily_logs', 'food_logs', 'exercise_logs']
            for table in tables:
                if DB_URI:
                    rows = execute_sql(f"SELECT * FROM {table}", fetch=True)
                    df_export = pd.DataFrame(rows) if rows else pd.DataFrame()
                else:
                    conn = get_db()
                    df_export = pd.read_sql_query(f"SELECT * FROM {table}", conn)
                    conn.close()
                if not df_export.empty:
                    csv_data = df_export.to_csv(index=False).encode('utf-8')
                    st.download_button(label=f"下载 {table}.csv", data=csv_data, file_name=f"{table}.csv", mime="text/csv")
        except Exception as e:
            st.error(f"导出失败: {e}")
