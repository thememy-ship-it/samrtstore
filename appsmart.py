import streamlit as st
import time
import bcrypt
import base64
import requests
import pandas as pd
import sqlite3
import re
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.base import JobLookupError

# 쿼리 파라미터 확인 (취소요청 전용 독립 팝업 창 모드인지 확인)
query_params = st.query_params
is_cancel_popup = query_params.get("cancel_popup") == "true"

st.set_page_config(
    page_title="🚨 긴급! 신규 발송전 취소요청 발생" if is_cancel_popup else "스마트스토어 올인원 컨트롤러",
    layout="wide",
    initial_sidebar_state="collapsed" if is_cancel_popup else "auto"
)
st.error(f"👉 네이버 등록용 현재 서버 IP: {requests.get('https://api.ipify.org').text}")
# ==========================================
# 0. 사운드 재생 HTML/JS 헬퍼 함수
# ==========================================
def play_alert_sound(sound_type):
    if sound_type == "order":
        js_code = """
        <script>
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        function playTone(freq, delay, dur) {
            setTimeout(() => {
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.type = 'triangle';
                osc.frequency.setValueAtTime(freq, ctx.currentTime);
                gain.gain.setValueAtTime(0.3, ctx.currentTime);
                gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + dur);
                osc.connect(gain);
                gain.connect(ctx.destination);
                osc.start();
                osc.stop(ctx.currentTime + dur);
            }, delay);
        }
        playTone(523.25, 0, 0.2);
        playTone(659.25, 120, 0.2);
        playTone(783.99, 240, 0.4);
        </script>
        """
    else:  # cancel
        js_code = """
        <script>
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        function playAlarm(freq, delay) {
            setTimeout(() => {
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.type = 'sawtooth';
                osc.frequency.setValueAtTime(freq, ctx.currentTime);
                gain.gain.setValueAtTime(0.4, ctx.currentTime);
                gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.18);
                osc.connect(gain);
                gain.connect(ctx.destination);
                osc.start();
                osc.stop(ctx.currentTime + 0.18);
            }, delay);
        }
        playAlarm(880, 0);
        playAlarm(440, 180);
        playAlarm(880, 360);
        playAlarm(440, 540);
        </script>
        """
    st.components.v1.html(js_code, height=0, width=0)

# ==========================================
# 1. 로컬 SQLite 데이터베이스
# ==========================================
DB_FILE = "schedules.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origin_product_no TEXT,
            channel_product_no TEXT DEFAULT '',
            product_name TEXT,
            before_price INTEGER DEFAULT 0,
            target_sale_price INTEGER DEFAULT 0,
            target_discount INTEGER DEFAULT 0,
            run_datetime TEXT,
            status TEXT DEFAULT '대기중',
            created_at TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS execution_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            executed_at TEXT,
            origin_product_no TEXT,
            action_type TEXT,
            target_price INTEGER,
            result_status TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS notified_alerts (
            order_id TEXT PRIMARY KEY,
            alert_type TEXT,
            notified_at TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def db_get_notified_order_ids(alert_type=None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if alert_type:
        c.execute("SELECT order_id FROM notified_alerts WHERE alert_type = ?", (alert_type,))
    else:
        c.execute("SELECT order_id FROM notified_alerts")
    rows = c.fetchall()
    conn.close()
    return set(r[0] for r in rows)

def db_add_notified_order_ids(order_ids, alert_type):
    if not order_ids:
        return
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for oid in order_ids:
        c.execute('''
            INSERT OR IGNORE INTO notified_alerts (order_id, alert_type, notified_at)
            VALUES (?, ?, ?)
        ''', (str(oid), alert_type, now_str))
    conn.commit()
    conn.close()

def db_add_schedule(origin_no, channel_no, prod_name, before_price, sale_price, discount, run_dt_str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute('''
        INSERT INTO schedules (origin_product_no, channel_product_no, product_name, before_price, target_sale_price, target_discount, run_datetime, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, '대기중', ?)
    ''', (str(origin_no), str(channel_no), str(prod_name), int(before_price), int(sale_price), int(discount), run_dt_str, created_at))
    sched_id = c.lastrowid
    conn.commit()
    conn.close()
    return sched_id

def db_get_schedules():
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query('''
        SELECT 
            id AS 예약ID,
            product_name AS "예약된 제품",
            channel_product_no AS "스토어 상품번호",
            origin_product_no AS "원상품번호",
            before_price AS "변경전 가격",
            (target_sale_price - target_discount) AS "변경후가격",
            target_sale_price AS "기본판매가",
            target_discount AS "변경할인가",
            run_datetime AS "예약 시간",
            status AS "상태"
        FROM schedules 
        WHERE status = '대기중' 
        ORDER BY run_datetime ASC
    ''', conn)
    conn.close()
    return df

def db_update_schedule_time(sched_id, new_dt_str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE schedules SET run_datetime = ? WHERE id = ?", (new_dt_str, sched_id))
    conn.commit()
    conn.close()

def db_update_schedule_status(sched_id, new_status):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE schedules SET status = ? WHERE id = ?", (new_status, sched_id))
    conn.commit()
    conn.close()

def db_delete_schedule(sched_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM schedules WHERE id = ?", (sched_id,))
    conn.commit()
    conn.close()

def db_add_log(prod_no, action_type, price, status_msg):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute('''
        INSERT INTO execution_logs (executed_at, origin_product_no, action_type, target_price, result_status)
        VALUES (?, ?, ?, ?, ?)
    ''', (now_str, str(prod_no), action_type, int(price), status_msg))
    conn.commit()
    conn.close()

def db_get_logs():
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query("SELECT executed_at AS 실행일시, origin_product_no AS 상품번호, action_type AS 구분, target_price AS 최종적용값, result_status AS 결과 FROM execution_logs ORDER BY id DESC LIMIT 50", conn)
    conn.close()
    return df

def db_clear_logs():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM execution_logs")
    conn.commit()
    conn.close()

# ==========================================
# 2. 네이버 API 통신 함수
# ==========================================
def get_access_token_silent(cid, csec):
    try:
        cid = cid.strip()
        csec = csec.strip()
        if not cid or not csec:
            return None
        timestamp = str(int((time.time() - 3) * 1000))
        password = f"{cid}_{timestamp}"
        hashed = bcrypt.hashpw(password.encode('utf-8'), csec.encode('utf-8'))
        client_secret_sign = base64.b64encode(hashed).decode('utf-8')
        url = "https://api.commerce.naver.com/external/v1/oauth2/token"
        data = {
            "client_id": cid,
            "timestamp": timestamp,
            "client_secret_sign": client_secret_sign,
            "grant_type": "client_credentials",
            "type": "SELF"
        }
        res = requests.post(url, data=data, timeout=5).json()
        return res.get("access_token")
    except Exception:
        return None

def update_product_discount_only(token, origin_product_no, discount_amount):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    get_url = f"https://api.commerce.naver.com/external/v2/products/origin-products/{origin_product_no}"
    try:
        get_res = requests.get(get_url, headers=headers, timeout=5)
    except Exception as e:
        return 0, f"네이버 연결 실패: {str(e)}"
    
    if get_res.status_code != 200:
        return get_res.status_code, f"조회 실패: {get_res.text}"
        
    try:
        data = get_res.json()
    except Exception:
        return get_res.status_code, f"파싱 실패: {get_res.text}"

    origin_product = data.get("originProduct", {})
    smartstore_channel_product = data.get("smartstoreChannelProduct", {})
    
    detail_attr = origin_product.get("detailAttribute", {})
    if "seoInfo" in detail_attr and "sellerTags" in detail_attr["seoInfo"]:
        detail_attr["seoInfo"]["sellerTags"] = []
        origin_product["detailAttribute"] = detail_attr

    customer_benefit = origin_product.get("customerBenefit", {})
    if discount_amount > 0:
        customer_benefit["immediateDiscountPolicy"] = {
            "discountMethod": {
                "value": int(discount_amount),
                "unitType": "WON"
            }
        }
    else:
        customer_benefit.pop("immediateDiscountPolicy", None)
        
    origin_product["customerBenefit"] = customer_benefit

    put_url = f"https://api.commerce.naver.com/external/v2/products/origin-products/{origin_product_no}"
    put_payload = {
        "originProduct": origin_product,
        "smartstoreChannelProduct": smartstore_channel_product
    }
    
    try:
        res = requests.put(put_url, headers=headers, json=put_payload, timeout=5)
        return res.status_code, res.json()
    except Exception as e:
        return 0, f"수정 요청 실패: {str(e)}"

def fetch_product_raw_data(token, origin_product_no):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    get_url = f"https://api.commerce.naver.com/external/v2/products/origin-products/{origin_product_no}"
    try:
        res = requests.get(get_url, headers=headers, timeout=6)
        if res.status_code == 200:
            return True, res.json()
        return False, res.text
    except Exception as e:
        return False, str(e)

def update_product_options(token, origin_product_no, full_data, updated_combinations):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    origin_product = full_data.get("originProduct", {})
    smartstore_channel_product = full_data.get("smartstoreChannelProduct", {})
    
    detail_attr = origin_product.get("detailAttribute", {})
    if "seoInfo" in detail_attr and "sellerTags" in detail_attr["seoInfo"]:
        detail_attr["seoInfo"]["sellerTags"] = []
        
    if "optionInfo" in detail_attr:
        detail_attr["optionInfo"]["optionCombinations"] = updated_combinations
    origin_product["detailAttribute"] = detail_attr

    put_url = f"https://api.commerce.naver.com/external/v2/products/origin-products/{origin_product_no}"
    put_payload = {
        "originProduct": origin_product,
        "smartstoreChannelProduct": smartstore_channel_product
    }
    try:
        res = requests.put(put_url, headers=headers, json=put_payload, timeout=6)
        return res.status_code, res.json()
    except Exception as e:
        return 0, f"요청 실패: {str(e)}"

# ==========================================
# 3. APScheduler 백그라운드 스케줄러 시계
# ==========================================
@st.cache_resource
def get_scheduler():
    scheduler = BackgroundScheduler(daemon=True, timezone="Asia/Seoul")
    scheduler.start()
    return scheduler

scheduler = get_scheduler()

def execute_scheduled_change(sched_id, cid, csec, origin_product_no, target_sale_price, target_discount):
    token = get_access_token_silent(cid, csec)
    if not token:
        db_update_schedule_status(sched_id, "토큰발급실패")
        db_add_log(origin_product_no, "예약실행", target_sale_price - target_discount, "토큰발급실패")
        return

    status, res = update_product_discount_only(token, origin_product_no, target_discount)
    if status == 200:
        db_update_schedule_status(sched_id, "실행완료")
        db_add_log(origin_product_no, "예약할인적용", target_sale_price - target_discount, "성공")
    else:
        err_msg = str(res)
        db_update_schedule_status(sched_id, "실패")
        db_add_log(origin_product_no, "예약할인적용", target_sale_price - target_discount, f"실패 ({err_msg[:30]})")

# ==========================================
# 4. 네이버 API 데이터 조회 함수들
# ==========================================
def get_access_token(cid, csec):
    cid = cid.strip()
    csec = csec.strip()
    if not cid or not csec:
        st.error("⚠️ 좌측 사이드바에 Client ID와 Client Secret을 먼저 입력해 주세요!")
        return None
    try:
        timestamp = str(int((time.time() - 3) * 1000))
        password = f"{cid}_{timestamp}"
        hashed = bcrypt.hashpw(password.encode('utf-8'), csec.encode('utf-8'))
        client_secret_sign = base64.b64encode(hashed).decode('utf-8')
        
        url = "https://api.commerce.naver.com/external/v1/oauth2/token"
        data = {
            "client_id": cid,
            "timestamp": timestamp,
            "client_secret_sign": client_secret_sign,
            "grant_type": "client_credentials",
            "type": "SELF"
        }
        res = requests.post(url, data=data, timeout=5).json()
        if "access_token" in res:
            return res["access_token"]
        st.error(f"❌ 네이버 인증 실패: {res.get('message', res)}")
        return None
    except Exception as e:
        st.error(f"❌ 인증 오류: {e}")
        return None

def fetch_product_by_origin_no(token, origin_no):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    detail_url = f"https://api.commerce.naver.com/external/v2/products/origin-products/{origin_no}"
    try:
        res = requests.get(detail_url, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            origin_prod = data.get("originProduct", {})
            ch_prod = data.get("smartstoreChannelProduct", {})
            sale_price = origin_prod.get("salePrice", 0)
            prod_name = origin_prod.get("name", "")
            stock_qty = origin_prod.get("stockQuantity", 0)
            channel_prod_no = ch_prod.get("channelProductNo") or origin_no
            raw_display = ch_prod.get("channelProductDisplayStatusType", "ON")
            display_status_kr = "전시중" if raw_display == "ON" else "전시중지"
            discount_amount = 0
            
            b1 = origin_prod.get("customerBenefit", {})
            d1 = b1.get("immediateDiscountPolicy", {}).get("discountMethod", {})
            b2 = ch_prod.get("customerBenefit", {})
            d2 = b2.get("immediateDiscountPolicy", {}).get("discountMethod", {})
            b3 = origin_prod.get("detailAttribute", {}).get("customerBenefit", {})
            d3 = b3.get("immediateDiscountPolicy", {}).get("discountMethod", {})
            
            target_disc = d1 or d2 or d3
            if target_disc:
                val = target_disc.get("value", 0)
                unit = target_disc.get("unitType")
                if unit == "WON":
                    discount_amount = val
                elif unit == "PERCENT":
                    discount_amount = int(sale_price * (val / 100))
                    
            final_sale_price = max(0, sale_price - discount_amount)
            return {
                "상품번호": str(channel_prod_no),
                "_원상품번호": str(origin_no),
                "기본 판매가(원)": sale_price,
                "판매자할인(원)": discount_amount,
                "🔥 최종할인가(원)": final_sale_price,
                "재고": stock_qty,
                "상품명": prod_name,
                "판매상태": "판매중",
                "전시상태": display_status_kr
            }
    except Exception:
        pass
    return None

def refresh_specific_products(token, origin_no_list):
    if "products_df" not in st.session_state or st.session_state["products_df"].empty:
        return
    unique_origins = list(set([str(o).strip() for o in origin_no_list if o]))
    if not unique_origins:
        return
    df = st.session_state["products_df"].copy()
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_product_by_origin_no, token, o_no): o_no for o_no in unique_origins}
        for future in as_completed(futures):
            item_data = future.result()
            if item_data:
                o_no = item_data["_원상품번호"]
                match_idx = df[df["_원상품번호"] == o_no].index
                if not match_idx.empty:
                    for col in item_data.keys():
                        df.loc[match_idx, col] = item_data[col]
    st.session_state["products_df"] = df
    st.session_state["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def fetch_single_detail(item, headers):
    origin_no = item.get("originProductNo")
    ch_list = item.get("channelProducts", [])
    ch = ch_list[0] if ch_list else {}
    channel_prod_no = ch.get("channelProductNo") or origin_no
    sale_price = ch.get("salePrice", 0)
    prod_name = ch.get("name", "")
    stock_qty = ch.get("stockQuantity", 0)
    discount_amount = 0
    display_status = ch.get("channelProductDisplayStatusType", "ON")
    display_status_kr = "전시중" if display_status == "ON" else "전시중지"
    
    discounted_price = ch.get("discountedPrice")
    if discounted_price is not None and 0 < discounted_price < sale_price:
        discount_amount = sale_price - discounted_price

    if discount_amount == 0:
        ch_benefit = ch.get("customerBenefit", {})
        imm_disc = ch_benefit.get("immediateDiscountPolicy", {}).get("discountMethod", {})
        if imm_disc:
            val = imm_disc.get("value", 0)
            unit = imm_disc.get("unitType")
            if unit == "WON":
                discount_amount = val
            elif unit == "PERCENT":
                discount_amount = int(sale_price * (val / 100))
    
    if discount_amount == 0 and origin_no:
        detail_url = f"https://api.commerce.naver.com/external/v2/products/origin-products/{origin_no}"
        for attempt in range(3):
            try:
                res = requests.get(detail_url, headers=headers, timeout=5)
                if res.status_code == 200:
                    data = res.json()
                    origin_prod = data.get("originProduct", {})
                    ch_prod = data.get("smartstoreChannelProduct", {})
                    raw_display = ch_prod.get("channelProductDisplayStatusType", "ON")
                    display_status_kr = "전시중" if raw_display == "ON" else "전시중지"
                    b1 = origin_prod.get("customerBenefit", {})
                    d1 = b1.get("immediateDiscountPolicy", {}).get("discountMethod", {})
                    b2 = ch_prod.get("customerBenefit", {})
                    d2 = b2.get("immediateDiscountPolicy", {}).get("discountMethod", {})
                    b3 = origin_prod.get("detailAttribute", {}).get("customerBenefit", {})
                    d3 = b3.get("immediateDiscountPolicy", {}).get("discountMethod", {})
                    target_disc = d1 or d2 or d3
                    if target_disc:
                        val = target_disc.get("value", 0)
                        unit = target_disc.get("unitType")
                        if unit == "WON":
                            discount_amount = val
                        elif unit == "PERCENT":
                            discount_amount = int(sale_price * (val / 100))
                    break
                elif res.status_code == 429:
                    time.sleep(0.3 * (attempt + 1))
            except Exception:
                time.sleep(0.2)
                
    final_sale_price = max(0, sale_price - discount_amount)
    return {
        "상품번호": str(channel_prod_no),
        "_원상품번호": str(origin_no),
        "기본 판매가(원)": sale_price,
        "판매자할인(원)": discount_amount,
        "🔥 최종할인가(원)": final_sale_price,
        "재고": stock_qty,
        "상품명": prod_name,
        "판매상태": "판매중",
        "전시상태": display_status_kr
    }

def fetch_all_products_safe(token, status_placeholder=None):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    search_url = "https://api.commerce.naver.com/external/v1/products/search"
    all_contents = []
    page = 1
    size = 100
    while True:
        payload = {"page": page, "size": size, "productStatusTypes": ["SALE"]}
        res = requests.post(search_url, headers=headers, json=payload, timeout=8)
        if res.status_code != 200:
            break
        data = res.json()
        contents = data.get("contents", [])
        if not contents:
            break
        all_contents.extend(contents)
        if len(all_contents) >= data.get("totalElements", 0):
            break
        page += 1
        
    total_count = len(all_contents)
    if total_count == 0:
        return pd.DataFrame()
        
    results = []
    completed = 0
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(fetch_single_detail, item, headers) for item in all_contents]
        for f in as_completed(futures):
            res_item = f.result()
            if res_item:
                results.append(res_item)
            completed += 1
            if status_placeholder:
                status_placeholder.info(f"⏳ 상품 정보 수집 중... ({completed} / {total_count}개 완료)")
                
    df = pd.DataFrame(results)
    if not df.empty:
        df = df.sort_values(by="판매자할인(원)", ascending=False).reset_index(drop=True)
    return df

def fetch_orders_and_cumulative_cancels(token):
    headers = {"Authorization": f"Bearer {token}"}
    kst = timezone(timedelta(hours=9))
    now_kst = datetime.now(kst)
    order_list, cancel_list, seen_po_ids = [], [], set()

    url_search = "https://api.commerce.naver.com/external/v1/pay-order/seller/product-orders/search"
    end_time_str = now_kst.strftime('%Y-%m-%dT%H:%M:%S.000+09:00')
    start_time_str = (now_kst - timedelta(hours=72)).strftime('%Y-%m-%dT%H:%M:%S.000+09:00')
    
    payed_po_ids = []
    cancel_po_ids = []

    try:
        r_payed = requests.get(url_search, headers=headers, params={
            "startTime": start_time_str,
            "endTime": end_time_str,
            "productOrderStatus": "PAYED"
        }, timeout=6)
        if r_payed.status_code == 200:
            for item in r_payed.json().get("data", {}).get("content", []):
                p_id = item.get("productOrderId")
                if p_id:
                    payed_po_ids.append(p_id)
        else:
            url_changed = "https://api.commerce.naver.com/external/v1/pay-order/seller/product-orders/last-changed-statuses"
            from_24h = (now_kst - timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%S.000+09:00')
            r_ch = requests.get(url_changed, headers=headers, params={"lastChangedFrom": from_24h}, timeout=5)
            if r_ch.status_code == 200:
                for item in r_ch.json().get("data", {}).get("lastChangeStatuses", []):
                    payed_po_ids.append(item["productOrderId"])
    except Exception:
        pass

    try:
        r_c1 = requests.get(url_search, headers=headers, params={
            "startTime": start_time_str,
            "endTime": end_time_str,
            "claimType": "CANCEL",
            "claimStatus": "CANCEL_REQUEST"
        }, timeout=5)
        if r_c1.status_code == 200:
            for item in r_c1.json().get("data", {}).get("content", []):
                p_id = item.get("productOrderId")
                if p_id:
                    cancel_po_ids.append(p_id)
    except Exception:
        pass

    try:
        r_c2 = requests.get(url_search, headers=headers, params={
            "startTime": start_time_str,
            "endTime": end_time_str,
            "productOrderStatus": "CANCEL_REQUEST"
        }, timeout=5)
        if r_c2.status_code == 200:
            for item in r_c2.json().get("data", {}).get("content", []):
                p_id = item.get("productOrderId")
                if p_id:
                    cancel_po_ids.append(p_id)
    except Exception:
        pass

    all_query_ids = list(set(payed_po_ids + cancel_po_ids))
    if not all_query_ids:
        return pd.DataFrame(), pd.DataFrame(), "조회된 내역 없음"

    detail_url = "https://api.commerce.naver.com/external/v1/pay-order/seller/product-orders/query"
    for i in range(0, len(all_query_ids), 50):
        chunk_ids = all_query_ids[i:i+50]
        try:
            detail_res = requests.post(detail_url, headers=headers, json={"productOrderIds": chunk_ids}, timeout=7)
            if detail_res.status_code == 200:
                for item in detail_res.json().get("data", []):
                    po = item.get("productOrder", {})
                    order = item.get("order", {})
                    p_id = str(po.get("productOrderId"))
                    if p_id in seen_po_ids:
                        continue
                    seen_po_ids.add(p_id)
                    status = po.get("productOrderStatus", "")
                    claim_type = po.get("claimType", "")
                    claim_status = po.get("claimStatus", "")
                    prod_id_str = str(po.get("productId") or po.get("channelProductNo") or "-")
                    
                    row_dict = {
                        "상품주문번호": p_id,
                        "스토어 상품번호": prod_id_str,
                        "주문상태": "결제완료(발주 전)" if status == "PAYED" else status,
                        "클레임상태": f"{claim_type} ({claim_status})" if claim_type else "-",
                        "상품명": po.get("productName"),
                        "옵션": po.get("productOption", "-"),
                        "수량": po.get("quantity"),
                        "결제금액": f"{po.get('totalPaymentAmount', 0):,}원",
                        "구매자명": order.get("ordererName"),
                        "수령인": po.get("shippingAddress", {}).get("name", "-"),
                        "결제일시": order.get("paymentDate") or po.get("claimDeliveryRequestDate", "-")
                    }
                    if claim_status == "CANCEL_REQUEST" or status == "CANCEL_REQUEST":
                        cancel_list.append(row_dict)
                    elif status == "PAYED":
                        order_list.append(row_dict)
        except Exception:
            pass

    return pd.DataFrame(order_list), pd.DataFrame(cancel_list), "성공"

def ship_product_order(token, product_order_id, delivery_company_code, tracking_number):
    url = "https://api.commerce.naver.com/external/v1/pay-order/seller/product-orders/dispatch"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "dispatchProductOrders": [{
            "productOrderId": str(product_order_id),
            "deliveryMethod": "DELIVERY",
            "deliveryCompanyCode": delivery_company_code,
            "trackingNumber": str(tracking_number)
        }]
    }
    res = requests.post(url, headers=headers, json=payload, timeout=5)
    return res.status_code, res.json()

def find_origin_no_by_input(input_no):
    input_str = str(input_no).strip()
    if "products_df" in st.session_state and not st.session_state["products_df"].empty:
        df = st.session_state["products_df"]
        matched = df[df["상품번호"] == input_str]
        if not matched.empty:
            return str(matched.iloc[0]["_원상품번호"])
        matched_org = df[df["_원상품번호"] == input_str]
        if not matched_org.empty:
            return input_str
    return input_str

# ==========================================
# 5. 🚨 발송전 취소요청 전용 독립 팝업 창
# ==========================================
if is_cancel_popup:
    st.markdown("## 🚨 [긴급] 신규 발송전 취소요청 접수 알림")
    st.error("⚠️ **구매자가 발송 전 주문 취소를 요청했습니다! 송장을 입력하지 마시고 스마트스토어 센터에서 승인/거부 처리를 진행하세요.**")
    
    if "cancels_df" in st.session_state and not st.session_state["cancels_df"].empty:
        st.dataframe(st.session_state["cancels_df"], use_container_width=True, height=320)
    else:
        st.info("현재 대기 중인 취소 요청 건이 없습니다.")

    st.markdown("""
        <div style="text-align: center; margin-top: 20px;">
            <a href="https://sell.smartstore.naver.com/#/naverpay/sale/cancel" target="_blank" style="background-color: #03c75a; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; font-size: 16px;">
                👉 네이버 스마트스토어 센터 취소관리 바로가기
            </a>
        </div>
    """, unsafe_allow_html=True)
    st.stop()

# ==========================================
# 6. 가격 일괄 편집 대화상자 (기본 판매자할인 수정용)
# ==========================================
@st.dialog("📋 선택한 제품 가격 일괄 편집 & 예약 센터", width="large")
def open_batch_edit_modal():
    if "modal_edit_df" not in st.session_state or st.session_state["modal_edit_df"].empty:
        st.warning("선택된 상품이 없습니다.")
        return

    st.caption("🔒 **기본 판매가는 정가로 고정**되며, **최종가격이나 판매자 할인을 수정하면 자동으로 계산되어 적용**됩니다.")
    m_col_left, m_col_right = st.columns([2.3, 1.2])
    
    with m_col_left:
        st.markdown("#### 1. 엑셀/시트에서 복사한 [최종가격 리스트] 한 번에 붙여넣기")
        paste_text = st.text_area(
            "엑셀에서 원하는 최종가격 열을 복사(Ctrl+C) 후 여기에 붙여넣기(Ctrl+V)",
            placeholder="506800\n657000\n1599000 ...",
            height=90
        )
        if st.button("⚡ 붙여넣은 최종가격 리스트를 아래 표에 일괄 적용하기"):
            if paste_text.strip():
                lines = [re.sub(r'[^0-9]', '', l.strip()) for l in paste_text.strip().split('\n') if l.strip()]
                num_lines = [int(x) for x in lines if x]
                df_curr = st.session_state["modal_edit_df"].copy()
                for i, target_final in enumerate(num_lines):
                    if i < len(df_curr):
                        base_p = int(df_curr.iloc[i]["기본 판매가"])
                        calc_discount = max(0, base_p - target_final)
                        df_curr.at[i, "판매자 할인"] = calc_discount
                        df_curr.at[i, "최종가격"] = target_final
                st.session_state["modal_edit_df"] = df_curr
                st.success(f"총 {min(len(num_lines), len(df_curr))}개 상품의 판매자할인이 자동 계산되었습니다!")
                st.rerun()

        st.markdown("#### 2. 선택한 제품 가격 표 (최종가격/판매자할인 직접 수정 가능)")
        edited_table = st.data_editor(
            st.session_state["modal_edit_df"],
            use_container_width=True,
            height=340,
            disabled=["상품번호", "선택한 제품", "기본 판매가", "_원상품번호"],
            column_config={
                "상품번호": st.column_config.TextColumn("스토어 상품번호"),
                "기본 판매가": st.column_config.NumberColumn("기본 판매가 (고정)", format="%d원"),
                "판매자 할인": st.column_config.NumberColumn("판매자 할인", step=100, format="%d원"),
                "최종가격": st.column_config.NumberColumn("최종가격 (수정시 할인 자동계산)", step=100, format="%d원")
            },
            key="modal_data_editor"
        )
        
        for idx in range(len(edited_table)):
            b_p = int(edited_table.iloc[idx]["기본 판매가"])
            cur_final = int(edited_table.iloc[idx]["최종가격"])
            cur_disc = int(edited_table.iloc[idx]["판매자 할인"])
            if cur_final != (b_p - cur_disc):
                calculated_discount = max(0, b_p - cur_final)
                edited_table.at[idx, "판매자 할인"] = calculated_discount

        st.session_state["modal_edit_df"] = edited_table
            
    with m_col_right:
        st.markdown("#### 3. 실행 방식 선택")
        modal_action = st.radio("어떻게 적용할까요?", options=["⚡ 즉시 바로 바꿀 옵션", "⏰ 특정 시각 예약 옵션"], index=0)
        
        if modal_action == "⚡ 즉시 바로 바꿀 옵션":
            st.write("스마트스토어의 기본 판매가는 유지하고, **판매자 할인만 즉시 변경**합니다.")
            if st.button("🚀 지금 즉시 일괄 적용", type="primary", use_container_width=True):
                token = get_access_token(client_id_input, client_secret_input)
                if not token:
                    st.error("❌ 네이버 인증 실패: 사이드바의 API 키를 확인해 주세요.")
                else:
                    s_cnt, f_cnt = 0, 0
                    fail_reasons = []
                    updated_origins = []
                    with st.spinner("네이버 스마트스토어에 판매자할인 적용 중..."):
                        for _, row_item in edited_table.iterrows():
                            raw_ch_no = str(row_item.get("상품번호", ""))
                            org_no = str(row_item.get("_원상품번호", "")) or find_origin_no_by_input(raw_ch_no)
                            p_base = int(row_item["기본 판매가"])
                            p_final = int(row_item["최종가격"])
                            p_disc = max(0, p_base - p_final)
                            status, res = update_product_discount_only(token, org_no, p_disc)
                            if status == 200:
                                s_cnt += 1
                                updated_origins.append(org_no)
                                db_add_log(raw_ch_no, "창일괄(할인만)", p_base - p_disc, "성공")
                            else:
                                f_cnt += 1
                                fail_reasons.append(f"[{raw_ch_no}] 거부 사유: {res}")
                                db_add_log(raw_ch_no, "창일괄(할인만)", p_base - p_disc, "실패")
                                
                    if updated_origins:
                        refresh_specific_products(token, updated_origins)

                    if f_cnt == 0:
                        st.session_state["modal_result_msg"] = f"🎉 즉시 반영 완료! (성공: {s_cnt}건 - 표 자동 갱신됨)"
                    else:
                        st.session_state["modal_result_msg"] = f"⚠️ 결과: 성공 {s_cnt}건 / 실패 {f_cnt}건"
                        st.session_state["modal_fail_list"] = fail_reasons
                    st.rerun()

        if "modal_result_msg" in st.session_state and st.session_state["modal_result_msg"]:
            st.info(st.session_state["modal_result_msg"])
            if "modal_fail_list" in st.session_state:
                for fr in st.session_state["modal_fail_list"]:
                    st.error(fr)
            if st.button("결과 확인 완료 (창 닫기)", use_container_width=True):
                st.session_state["modal_result_msg"] = ""
                st.session_state["modal_fail_list"] = []
                st.rerun()
                    
        else:
            st.write("지정한 날짜/시간에 백그라운드에서 자동 실행됩니다.")
            m_now = datetime.now()
            res_d = st.date_input("예약 날짜", value=m_now.date(), key="modal_d")
            res_t = st.time_input("예약 시간", value=(m_now + timedelta(minutes=5)).time(), key="modal_t")
            scheduled_dt = datetime.combine(res_d, res_t)
            st.caption(f"📌 예약 시각: **{scheduled_dt.strftime('%Y-%m-%d %H:%M')}**")
            
            if st.button("💾 이 시각에 일괄 예약 등록", type="primary", use_container_width=True):
                if scheduled_dt <= datetime.now():
                    st.error("❌ 현재 시각 이전으로는 예약할 수 없습니다.")
                elif not client_id_input or not client_secret_input:
                    st.error("⚠️ 좌측 사이드바에 API 키를 입력해 주세요.")
                else:
                    run_str = scheduled_dt.strftime("%Y-%m-%d %H:%M:%S")
                    reg_cnt = 0
                    for _, row_item in edited_table.iterrows():
                        ch_no = str(row_item["상품번호"])
                        org_no = str(row_item.get("_원상품번호") or find_origin_no_by_input(ch_no))
                        p_name = str(row_item["선택한 제품"])
                        p_before = int(row_item.get("기존최종가", row_item["기본 판매가"]))
                        p_base = int(row_item["기본 판매가"])
                        p_final = int(row_item["최종가격"])
                        p_disc = max(0, p_base - p_final)
                        
                        sched_id = db_add_schedule(org_no, ch_no, p_name, p_before, p_base, p_disc, run_str)
                        job_id = f"job_schedule_{sched_id}"
                        scheduler.add_job(
                            execute_scheduled_change,
                            trigger="date",
                            run_date=scheduled_dt,
                            args=[sched_id, client_id_input.strip(), client_secret_input.strip(), org_no, p_base, p_disc],
                            id=job_id,
                            replace_existing=True
                        )
                        reg_cnt += 1
                    st.success(f"🎉 총 {reg_cnt}개 상품의 할인 예약이 등록되었습니다! ({run_str} 실행)")
                    st.rerun()

# ==========================================
# 🔴 7. [대폭 업그레이드]: 다중 제품 옵션가 동시 편집 대화상자 (제품별 구획 완벽 분리)
# ==========================================
@st.dialog("⚙️ 선택한 제품들 옵션가 동시 편집 센터 (제품별 구획 분리)", width="large")
def open_multi_product_options_modal():
    selected_items = st.session_state.get("selected_option_products_list", [])
    if not selected_items:
        st.warning("선택된 상품이 없습니다.")
        return

    token = get_access_token(client_id_input, client_secret_input)
    if not token:
        st.error("⚠️ 좌측 사이드바에 API 키를 먼저 입력해 주세요.")
        return

    # 다중 제품의 옵션 데이터 준비
    if "multi_opt_data" not in st.session_state:
        st.session_state["multi_opt_data"] = {}

    # 선택된 각 제품별로 옵션 원천 데이터 조회
    all_has_options = False
    with st.spinner(f"선택한 {len(selected_items)}개 제품의 옵션 정보를 불러오는 중..."):
        for prod in selected_items:
            org_no = prod["_원상품번호"]
            if org_no not in st.session_state["multi_opt_data"]:
                ok, raw_json = fetch_product_raw_data(token, org_no)
                if ok:
                    opt_info = raw_json.get("originProduct", {}).get("detailAttribute", {}).get("optionInfo", {})
                    combinations = opt_info.get("optionCombinations", [])
                    
                    base_p = int(prod["기본 판매가(원)"])
                    disc_p = int(prod["판매자할인(원)"])
                    base_final = base_p - disc_p
                    
                    rows = []
                    if combinations:
                        all_has_options = True
                        for c_item in combinations:
                            c_id = c_item.get("id")
                            name_parts = [c_item.get(f"optionName{i}") for i in range(1, 5) if c_item.get(f"optionName{i}")]
                            full_name = " / ".join(name_parts) if name_parts else "옵션"
                            opt_p = c_item.get("price", 0)
                            stock_q = c_item.get("stockQuantity", 0)
                            rows.append({
                                "_id": c_id,
                                "옵션 상세명": full_name,
                                "현재 옵션가": opt_p,
                                "변경할 옵션가(원)": opt_p,
                                "재고": stock_q,
                                "🔥 예상 최종 구매가": base_final + opt_p
                            })
                    
                    st.session_state["multi_opt_data"][org_no] = {
                        "info": prod,
                        "raw_json": raw_json,
                        "df": pd.DataFrame(rows),
                        "base_price": base_p,
                        "base_final": base_final,
                        "has_options": bool(combinations)
                    }

    st.caption("선택한 각 제품마다 **독립된 카드 상자(Border)**로 분리되어 있습니다. 표에서 숫자를 직접 바꾸거나 엑셀 열을 붙여넣어 일괄 적용하세요.")

    total_invalid = False
    
    # 🔴 [핵심]: 각 제품마다 명확하게 구획이 나뉘는 독립 컨테이너 카드 렌더링
    for org_no, p_data in st.session_state["multi_opt_data"].items():
        prod_meta = p_data["info"]
        base_p = p_data["base_price"]
        base_final = p_data["base_final"]
        min_p = -int(base_p * 0.5)
        max_p = int(base_p * 0.5)
        
        with st.container(border=True):
            st.markdown(f"### 📦 **[{prod_meta['상품번호']}] {prod_meta['상품명']}**")
            c_info1, c_info2, c_info3 = st.columns([1.5, 1.5, 2.5])
            with c_info1:
                st.write(f"• 기본 판매가: **{base_p:,}원**")
            with c_info2:
                st.write(f"• 현재 판매자할인가: **{base_final:,}원**")
            with c_info3:
                st.info(f"💡 허용 옵션가 범위: **{min_p:,}원 ~ +{max_p:,}원** (-50% ~ +50%)")

            if not p_data["has_options"] or p_data["df"].empty:
                st.warning("⚠️ 이 상품은 조합형 옵션이 없는 단품 상품입니다. (수정 불필요)")
                continue

            # 제품별 엑셀 붙여넣기 기능
            paste_col1, paste_col2 = st.columns([3, 1.5])
            with paste_col1:
                p_text = st.text_input(
                    f"엑셀 옵션가 열 복사 후 붙여넣기 (엔터/적용)",
                    placeholder="0, 3000, 5000 또는 줄바꿈된 숫자들",
                    key=f"paste_opt_{org_no}"
                )
            with paste_col2:
                if st.button("⚡ 이 제품에 적용", key=f"btn_paste_{org_no}"):
                    if p_text.strip():
                        nums = [int(re.sub(r'[^0-9\-]', '', x)) for x in re.split(r'[\n,\s]+', p_text.strip()) if re.sub(r'[^0-9\-]', '', x)]
                        curr_df = p_data["df"].copy()
                        for i, n_val in enumerate(nums):
                            if i < len(curr_df):
                                curr_df.at[i, "변경할 옵션가(원)"] = n_val
                                curr_df.at[i, "🔥 예상 최종 구매가"] = base_final + n_val
                        p_data["df"] = curr_df
                        st.rerun()

            # 제품별 독립 옵션 에디터
            edited_df = st.data_editor(
                p_data["df"],
                use_container_width=True,
                height=230,
                disabled=["_id", "옵션 상세명", "현재 옵션가", "재고", "🔥 예상 최종 구매가"],
                column_config={
                    "_id": None,
                    "옵션 상세명": st.column_config.TextColumn("옵션 상세명"),
                    "현재 옵션가": st.column_config.NumberColumn("현재 옵션가", format="%d원"),
                    "변경할 옵션가(원)": st.column_config.NumberColumn("변경할 옵션 추가금", step=100, format="%d원"),
                    "재고": st.column_config.NumberColumn("재고"),
                    "🔥 예상 최종 구매가": st.column_config.NumberColumn("최종 소비자 구매가", format="%d원")
                },
                key=f"editor_opt_{org_no}"
            )

            # 실시간 최종가 연동
            for idx in range(len(edited_df)):
                v_p = int(edited_df.iloc[idx]["변경할 옵션가(원)"])
                edited_df.at[idx, "🔥 예상 최종 구매가"] = base_final + v_p
            p_data["df"] = edited_df

            # 제품별 네이버 규정 검사
            prod_invalid = []
            for _, r in edited_df.iterrows():
                v_p = int(r["변경할 옵션가(원)"])
                if v_p < min_p or v_p > max_p:
                    prod_invalid.append(f"[{r['옵션 상세명']}]: {v_p:,}원 (허용: {min_p:,}원 ~ {max_p:,}원)")
                    total_invalid = True
            
            if prod_invalid:
                st.error(f"❌ 규정 범위 초과: {', '.join(prod_invalid)}")

    st.markdown("---")
    
    # 🔴 [핵심]: 선택한 모든 제품의 옵션 가격을 한 번에 동시 반영하는 버튼
    if st.button("🚀 선택한 모든 제품의 옵션가 동시 일괄 반영", type="primary", use_container_width=True, disabled=total_invalid):
        success_count, fail_count = 0, 0
        fail_msgs = []
        
        with st.spinner("선택된 모든 제품의 옵션가를 스마트스토어에 반영 중..."):
            for org_no, p_data in st.session_state["multi_opt_data"].items():
                if not p_data["has_options"]:
                    continue
                
                raw_json = p_data["raw_json"]
                edited_df = p_data["df"]
                raw_comb = raw_json.get("originProduct", {}).get("detailAttribute", {}).get("optionInfo", {}).get("optionCombinations", [])
                
                for c_item in raw_comb:
                    match_row = edited_df[edited_df["_id"] == c_item.get("id")]
                    if not match_row.empty:
                        c_item["price"] = int(match_row.iloc[0]["변경할 옵션가(원)"])
                        
                code, res_b = update_product_options(token, org_no, raw_json, raw_comb)
                p_no = p_data["info"]["상품번호"]
                
                if code == 200:
                    success_count += 1
                    db_add_log(p_no, "다중옵션수정", p_data["base_final"], "성공")
                else:
                    fail_count += 1
                    fail_msgs.append(f"[{p_no}] 거부 사유: {res_b}")
                    db_add_log(p_no, "다중옵션수정", p_data["base_final"], f"실패: {res_b}")
                    
        # 완료 후 캐시 초기화
        st.session_state.pop("multi_opt_data", None)
        
        if fail_count == 0:
            st.success(f"🎉 선택한 모든 제품(총 {success_count}개)의 옵션 가격이 성공적으로 반영되었습니다!")
        else:
            st.warning(f"결과: 성공 {success_count}개 / 실패 {fail_count}개")
            for fm in fail_msgs:
                st.error(fm)

# ==========================================
# 8. 메인 화면 구성
# ==========================================
st.sidebar.header("🔑 네이버 API 키 입력")
client_id_input = st.sidebar.text_input("Client ID", value="")
client_secret_input = st.sidebar.text_input("Client Secret", type="password", value="")

tab1, tab2, tab3 = st.tabs(["💰 메인 상품 조회 및 제어판", "📋 예약 관리 및 변경 현황", "🚨 주문·취소 실시간 사운드 감시 센터"])

# ------------------------------------------
# 탭 1: 메인 상품 조회 & 다중 옵션 편집 연동
# ------------------------------------------
with tab1:
    st.subheader("내 스토어 상품 목록 (판매중 전량)")
    btn_col1, btn_col2 = st.columns([1, 4])
    with btn_col1:
        if st.button("⚡ 판매중 전 상품 불러오기", type="primary", use_container_width=True):
            token = get_access_token(client_id_input, client_secret_input)
            if token:
                status_placeholder = st.empty()
                df = fetch_all_products_safe(token, status_placeholder)
                status_placeholder.empty()
                st.session_state["products_df"] = df
                st.session_state["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                st.success(f"총 {len(df)}개의 판매중 상품을 성공적으로 불러왔습니다!")
                    
    with btn_col2:
        if "last_updated" in st.session_state:
            st.caption(f"🕒 마지막 갱신 일시: {st.session_state['last_updated']}")
                
    if "products_df" in st.session_state and not st.session_state["products_df"].empty:
        search_query = st.text_input(
            "🔍 검색 (스마트스토어 상품번호를 쉼표/공백으로 여러 개 입력하거나 제품명 검색)",
            placeholder="예: 센터에 적힌 상품번호 쉼표 입력 / 또는 드롱기, 커피머신"
        )
        
        display_df = st.session_state["products_df"].copy()
        if search_query.strip():
            raw_input = search_query.strip()
            found_numbers = re.findall(r'\b\d{8,}\b', raw_input)
            if found_numbers:
                display_df = display_df[
                    display_df["상품번호"].isin(found_numbers) | 
                    display_df["_원상품번호"].isin(found_numbers)
                ].reset_index(drop=True)
                st.caption(f"🔢 입력된 상품번호와 일치하는 상품 총 **{len(display_df)}개**")
            else:
                display_df = display_df[display_df["상품명"].str.contains(raw_input, case=False, na=False)].reset_index(drop=True)
                st.caption(f"🔎 검색어 **'{raw_input}'**와 일치하는 상품 총 **{len(display_df)}개**")

        st.info("💡 **원하는 상품들을 체크(1개 또는 여러 개)**한 뒤 아래에서 `할인 일괄 편집` 또는 `옵션가 일괄 편집` 버튼을 누르세요!")
        show_columns = ["상품번호", "기본 판매가(원)", "판매자할인(원)", "🔥 최종할인가(원)", "재고", "상품명", "판매상태", "전시상태"]
        
        event = st.dataframe(
            display_df[show_columns],
            use_container_width=True,
            height=650,
            on_select="rerun",
            selection_mode="multi-row"
        )
        
        selected_rows_data = pd.DataFrame()
        if event and event.selection and event.selection.rows:
            selected_indices = event.selection.rows
            selected_rows_data = display_df.iloc[selected_indices]
            
        b_col1, b_col2, b_col3 = st.columns([1.5, 1.8, 2.5])
        with b_col1:
            if st.button("📥 선택 제품 할인 일괄 편집", type="primary", use_container_width=True):
                if not selected_rows_data.empty:
                    modal_df = pd.DataFrame({
                        "상품번호": selected_rows_data["상품번호"].tolist(),
                        "_원상품번호": selected_rows_data["_원상품번호"].tolist(),
                        "선택한 제품": selected_rows_data["상품명"].tolist(),
                        "기본 판매가": selected_rows_data["기본 판매가(원)"].tolist(),
                        "판매자 할인": selected_rows_data["판매자할인(원)"].tolist(),
                        "최종가격": selected_rows_data["🔥 최종할인가(원)"].tolist(),
                        "기존최종가": selected_rows_data["🔥 최종할인가(원)"].tolist()
                    })
                    st.session_state["modal_edit_df"] = modal_df
                    open_batch_edit_modal()
                else:
                    st.warning("먼저 위 표에서 체크박스를 1개 이상 선택해 주세요.")

        # 🔴 [핵심 기능 버튼]: 여러 상품을 체크해도 제품별로 칸이 확실히 나뉘어 뜨는 옵션 편집 모달
        with b_col2:
            if st.button("⚙️ 선택 제품들 옵션가 일괄 편집", use_container_width=True):
                if not selected_rows_data.empty:
                    # 다중 선택된 제품 목록 세션에 저장
                    st.session_state["selected_option_products_list"] = [r.to_dict() for _, r in selected_rows_data.iterrows()]
                    st.session_state.pop("multi_opt_data", None)  # 새 선택에 맞게 캐시 리셋
                    open_multi_product_options_modal()
                else:
                    st.warning("먼저 위 표에서 옵션을 수정할 상품을 1개 이상 체크해 주세요.")

        with b_col3:
            if not selected_rows_data.empty:
                st.caption(f"현재 선택된 상품: **총 {len(selected_rows_data)}개**")
    else:
        st.info("👈 좌측 사이드바에 키를 넣고 **[⚡ 판매중 전 상품 불러오기]**를 클릭하세요.")

    st.markdown("---")
    log_title_col, log_btn_col = st.columns([3, 1])
    with log_title_col:
        st.subheader("📜 최근 가격 변경 실행 기록")
    with log_btn_col:
        if st.button("🗑️ 기록 전체 초기화", use_container_width=True):
            db_clear_logs()
            st.success("✅ 최근 가격 변경 실행 기록이 모두 삭제되었습니다!")
            st.rerun()

    logs_df = db_get_logs()
    if not logs_df.empty:
        st.dataframe(logs_df, use_container_width=True, height=220)
    else:
        st.caption("기록이 비어 있습니다. (초기화 완료)")

# ------------------------------------------
# 탭 2: 예약 관리 및 변경 현황
# ------------------------------------------
with tab2:
    st.subheader("📋 예약 관리 및 예약된 제품 현황")
    st.caption("현재 예약 대기 중인 제품들의 목록입니다. 취소 버튼을 누르면 즉시 예약이 취소되며, 시간을 클릭하여 변경할 수 있습니다.")
    sched_df = db_get_schedules()
    if not sched_df.empty:
        display_sched = sched_df[["예약ID", "예약된 제품", "스토어 상품번호", "변경전 가격", "변경후가격", "예약 시간"]].copy()
        display_sched["변경전 가격"] = display_sched["변경전 가격"].apply(lambda x: f"{x:,}원")
        display_sched["변경후가격"] = display_sched["변경후가격"].apply(lambda x: f"{x:,}원")
        st.dataframe(display_sched, use_container_width=True, height=320)
        st.markdown("---")
        act_c1, act_c2 = st.columns([1.5, 2.5])
        with act_c1:
            st.markdown("#### ❌ 예약 취소")
            cancel_options = sched_df["예약ID"].tolist()
            selected_cancel_id = st.selectbox("취소할 예약 ID 선택", options=cancel_options)
            if st.button("🚨 선택한 예약 즉시 취소", type="primary", use_container_width=True):
                try:
                    scheduler.remove_job(f"job_schedule_{selected_cancel_id}")
                except JobLookupError:
                    pass
                db_delete_schedule(selected_cancel_id)
                st.success(f"예약 ID [{selected_cancel_id}]가 정상 취소되었습니다!")
                st.rerun()

        with act_c2:
            st.markdown("#### ⏰ 예약 시간 변경")
            modify_id = st.selectbox("시간을 변경할 예약 ID 선택", options=cancel_options, key="mod_sel")
            target_row = sched_df[sched_df["예약ID"] == modify_id].iloc[0]
            st.info(f"선택 제품: **{target_row['예약된 제품']}** (현재 예약 시각: `{target_row['예약 시간']}`)")
            t_col1, t_col2 = st.columns(2)
            with t_col1:
                new_date = st.date_input("새로운 날짜", value=datetime.now().date(), key="new_d")
            with t_col2:
                new_time = st.time_input("새로운 시간", value=(datetime.now() + timedelta(minutes=10)).time(), key="new_t")
            new_target_dt = datetime.combine(new_date, new_time)
            if st.button("🔄 새로운 시각으로 예약 시간 갱신", use_container_width=True):
                if new_target_dt <= datetime.now():
                    st.error("현재 시각 이전으로는 변경할 수 없습니다.")
                else:
                    new_dt_str = new_target_dt.strftime("%Y-%m-%d %H:%M:%S")
                    job_id = f"job_schedule_{modify_id}"
                    try:
                        scheduler.reschedule_job(job_id, trigger="date", run_date=new_target_dt)
                    except JobLookupError:
                        pass
                    db_update_schedule_time(modify_id, new_dt_str)
                    st.success(f"예약 ID [{modify_id}]의 실행 시간이 **{new_dt_str}**로 성공적으로 변경되었습니다!")
                    st.rerun()
    else:
        st.info("현재 대기 중인 예약이 없습니다. 탭 1에서 상품을 선택하여 예약을 등록해 보세요.")

# ------------------------------------------
# 탭 3: 주문·취소 실시간 사운드 감시 센터 (발주 전 주문 전용)
# ------------------------------------------
with tab3:
    st.subheader("🚨 주문·취소 실시간 사운드 감시 센터")
    
    sync_c1, sync_c2, sync_c3, sync_c4 = st.columns([1.5, 1.2, 1.5, 2])
    with sync_c1:
        sound_monitor_active = st.checkbox("🔔 실시간 감시 활성화 (신규 주문/취소)", value=False, key="sound_monitor_chk")
    with sync_c2:
        order_refresh_sec = st.selectbox(
            "감시 주기",
            options=[60, 180, 300],
            index=0,
            format_func=lambda x: f"{x}초마다" if x < 60 else f"{x//60}분마다",
            key="sound_interval"
        )
    with sync_c3:
        st.write("🔊 사운드 테스트:")
        t_b1, t_b2 = st.columns(2)
        with t_b1:
            if st.button("벨(주문)", help="신규 주문 띠링~"):
                play_alert_sound("order")
        with t_b2:
            if st.button("사이렌(취소)", help="발송전 취소 사이렌"):
                play_alert_sound("cancel")

    with sync_c4:
        if "order_last_updated" in st.session_state:
            st.caption(f"🕒 마지막 자동 감시: {st.session_state['order_last_updated']}")

    if sound_monitor_active:
        token = get_access_token_silent(client_id_input, client_secret_input)
        if token:
            cur_orders_df, cur_cancels_df, _ = fetch_orders_and_cumulative_cancels(token)
            
            cur_order_ids = set(cur_orders_df["상품주문번호"].tolist()) if not cur_orders_df.empty else set()
            cur_cancel_ids = set(cur_cancels_df["상품주문번호"].tolist()) if not cur_cancels_df.empty else set()
            
            notified_orders = db_get_notified_order_ids("ORDER")
            notified_cancels = db_get_notified_order_ids("CANCEL")
            
            if "monitor_initialized" not in st.session_state:
                db_add_notified_order_ids(cur_order_ids, "ORDER")
                db_add_notified_order_ids(cur_cancel_ids, "CANCEL")
                st.session_state["monitor_initialized"] = True
            else:
                brand_new_cancels = cur_cancel_ids - notified_cancels
                brand_new_orders = cur_order_ids - notified_orders
                
                if brand_new_cancels:
                    new_cnt = len(brand_new_cancels)
                    st.session_state["alert_type"] = "cancel"
                    st.session_state["alert_msg"] = f"🚨 긴급! [신규 발송전 취소요청] {new_cnt}건이 새로 접수되었습니다! 즉시 확인하세요."
                    play_alert_sound("cancel")
                    db_add_notified_order_ids(brand_new_cancels, "CANCEL")
                    st.components.v1.html(
                        """
                        <script>
                        window.open(window.location.origin + window.location.pathname + '?cancel_popup=true', 'cancel_alert_window', 'width=950,height=550,resizable=yes,scrollbars=yes');
                        </script>
                        """,
                        height=0,
                        width=0
                    )
                elif brand_new_orders:
                    new_cnt = len(brand_new_orders)
                    st.session_state["alert_type"] = "order"
                    st.session_state["alert_msg"] = f"🎉 띵동! [신규 발주전 주문] {new_cnt}건이 새로 결제되었습니다!"
                    play_alert_sound("order")
                    db_add_notified_order_ids(brand_new_orders, "ORDER")

            st.session_state["orders_df"] = cur_orders_df
            st.session_state["cancels_df"] = cur_cancels_df
            st.session_state["order_last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if "alert_msg" in st.session_state and st.session_state["alert_msg"]:
        if st.session_state.get("alert_type") == "cancel":
            st.error(st.session_state["alert_msg"])
        else:
            st.success(st.session_state["alert_msg"])
        if st.button("알림 확인 완료 (팝업 닫기)"):
            st.session_state["alert_msg"] = ""
            st.rerun()

    st.markdown("---")

    kpi1, kpi2 = st.columns(2)
    orders_cnt = len(st.session_state.get("orders_df", []))
    cancels_cnt = len(st.session_state.get("cancels_df", []))
    
    with kpi1:
        st.metric(label="🛒 신규주문 (발주 전 대기 건)", value=f"{orders_cnt} 건")
    with kpi2:
        st.metric(label="⚠️ 발송전 취소요청 (누적 미처리 대기 건)", value=f"{cancels_cnt} 건", delta="🚨 취소확인" if cancels_cnt > 0 else "정상")

    st.markdown("---")

    b_act1, b_act2 = st.columns([1.5, 2])
    with b_act1:
        if st.button("🔍 수동으로 주문·취소 즉시 조회하기", type="primary", use_container_width=True):
            token = get_access_token(client_id_input, client_secret_input)
            if token:
                with st.spinner("네이버 스마트스토어 발주전 주문 및 누적 취소 데이터를 수집 중..."):
                    o_df, c_df, msg = fetch_orders_and_cumulative_cancels(token)
                    st.session_state["orders_df"] = o_df
                    st.session_state["cancels_df"] = c_df
                    st.session_state["order_last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    
                    cur_o_ids = set(o_df["상품주문번호"].tolist()) if not o_df.empty else set()
                    cur_c_ids = set(c_df["상품주문번호"].tolist()) if not c_df.empty else set()
                    db_add_notified_order_ids(cur_o_ids, "ORDER")
                    db_add_notified_order_ids(cur_c_ids, "CANCEL")
                    
                    st.success("데이터 조회가 완료되었습니다!")
                    
    with b_act2:
        if cancels_cnt > 0:
            if st.button("🚨 대기 중인 발송전 취소요청 독립 팝업창 열기", use_container_width=True):
                st.components.v1.html(
                    """
                    <script>
                    window.open(window.location.origin + window.location.pathname + '?cancel_popup=true', 'cancel_alert_window', 'width=950,height=550,resizable=yes,scrollbars=yes');
                    </script>
                    """,
                    height=0,
                    width=0
                )

    sub_t1, sub_t2 = st.tabs(["🛒 신규주문 목록 (발주 전 대기 건)", "⚠️ 발송전 취소요청 목록 (누적 미처리)"])
    
    with sub_t1:
        o_col1, o_col2 = st.columns([2.3, 1.2])
        with o_col1:
            if "orders_df" in st.session_state and not st.session_state["orders_df"].empty:
                st.dataframe(st.session_state["orders_df"], use_container_width=True)
            else:
                st.caption("현재 발주확인 전인 신규 주문 내역이 없습니다.")
        with o_col2:
            st.subheader("🚚 원클릭 송장 등록 및 발송")
            t_oid = st.text_input("상품주문번호 입력", key="ship_oid")
            c_options = {"CJ대한통운": "CJGLS", "한진택배": "HANJIN", "롯데택배": "LOTTE", "우체국택배": "EPOST", "로젠택배": "KGB"}
            s_courier = st.selectbox("택배사", options=list(c_options.keys()), key="ship_c")
            t_no = st.text_input("운송장 번호", key="ship_tno")
            if st.button("📤 즉시 발송 처리", use_container_width=True):
                token = get_access_token(client_id_input, client_secret_input)
                if token and t_oid and t_no:
                    status, res = ship_product_order(token, t_oid, c_options[s_courier], t_no)
                    if status == 200:
                        st.success(f"주문 [{t_oid}] 발송 처리가 완료되었습니다!")
                    else:
                        st.error(f"실패: {res}")

    with sub_t2:
        if "cancels_df" in st.session_state and not st.session_state["cancels_df"].empty:
            st.error("🚨 아래 취소요청 건은 송장을 넣지 마시고 스마트스토어 센터에서 승인/거부 처리를 진행하세요!")
            st.dataframe(st.session_state["cancels_df"], use_container_width=True)
        else:
            st.info("현재 접수된 발송전 취소요청이 없습니다. (0건 정상)")

    if sound_monitor_active:
        time.sleep(order_refresh_sec)
        st.rerun()
