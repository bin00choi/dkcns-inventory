import os
import re
import glob
import pandas as pd
import gspread
import streamlit as st
from oauth2client.service_account import ServiceAccountCredentials
import google.generativeai as genai

# =========================================================================
# [설정] 🔑 발급받으신 Gemini API Key를 여기에 직접 채워주세요!
# =========================================================================
GEMINI_API_KEY = "AQ.Ab8RN6JdegccPde7txASjVxPBOPRHDL40gaKhAP7_C1uhOYp5w" 

# 웹 페이지 기본 설정 (스마트폰 모바일 최적화 및 넓은 화면 모드)
st.set_page_config(page_title="DKCNS 실시간 재고 에이전트", page_icon="📦", layout="wide")

# API 키 자동 정제 (앞뒤 공백, 줄바꿈, 잘못 들어간 따옴표 완벽 제거)
CLEAN_API_KEY = str(GEMINI_API_KEY).strip().replace("'", "").replace('"', "")

# 1. 폴더 내 특정 접두사로 시작하는 가장 최신 파일 탐색 함수
def get_latest_file(folder_path, prefix):
    search_pattern = os.path.join(folder_path, f"{prefix}*.*")
    files = [
        f for f in glob.glob(search_pattern) 
        if f.lower().endswith(('.xlsx', '.xls', '.csv')) and not os.path.basename(f).startswith('~$')
    ]
    if not files:
        return None
    return max(files, key=os.path.getmtime)

# 2. 기호 정규화 함수
def normalize_code(code_val):
    if pd.isna(code_val):
        return ""
    return re.sub(r'[-_\s/()\\\[\]]', '', str(code_val)).upper()

# 3. Excel 날짜 숫자를 YY-MM-DD 문자열로 파싱하는 함수
def parse_excel_date(val):
    if pd.isna(val) or val == "":
        return ""
    try:
        num = float(val)
        return pd.to_datetime(num, unit='D', origin='1899-12-30').strftime('%y-%m-%d')
    except:
        try:
            return pd.to_datetime(str(val).strip()).strftime('%y-%m-%d')
        except:
            return str(val).strip()

# 4. 실시간 재고 대사 진행 함수 (정확한 대사 및 PyArrow 안정성 보강 버전)
def load_local_reconciled_data():
    TARGET_FOLDER = r"D:\0004.재고\재고파일"
    
    latest_daewon = get_latest_file(TARGET_FOLDER, "재고현황조회_")
    latest_erp = get_latest_file(TARGET_FOLDER, "CCAPEC06600_")
    
    if not latest_daewon or not latest_erp:
        st.error("🚨 'D:\\0004.재고\\재고파일' 폴더에 대원 재고 파일 또는 ERP 재고 파일이 존재하지 않습니다.")
        st.stop()
        
    df_daewon = pd.read_excel(latest_daewon) if latest_daewon.lower().endswith(('.xlsx', '.xls')) else pd.read_csv(latest_daewon)
    df_erp = pd.read_excel(latest_erp) if latest_erp.lower().endswith(('.xlsx', '.xls')) else pd.read_csv(latest_erp)
    
    # 구품목코드 및 보유개월수 컬럼 탐색
    erp_gu_col = [c for c in df_erp.columns if '구품목코드' in c]
    gu_col_name = erp_gu_col[0] if erp_gu_col else '구품목코드\n(품목명2)'
    
    erp_month_col = [c for c in df_erp.columns if '보유' in c and '개월' in c]
    month_col_name = erp_month_col[0] if erp_month_col else '보유\n개월수'
    
    # 정규화 임시 열 생성
    df_daewon['clean_code'] = df_daewon['업체상품코드'].apply(normalize_code)
    df_erp['clean_p_name'] = df_erp['품명'].apply(normalize_code)
    df_erp['clean_gu_code'] = df_erp[gu_col_name].apply(normalize_code)
    
    results = []
    for idx, row in df_daewon.iterrows():
        clean_key = row['clean_code']
        raw_code = row['업체상품코드']
        
        dw_item_code = row.get('상품코드', '')
        dw_item_name = row.get('상품명', '')
        dw_cur_qty = row.get('현재재고', 0)
        dw_avail_qty = row.get('가용재고', 0)
        
        erp_match = df_erp[df_erp['clean_p_name'] == clean_key]
        is_old_item = False
        
        if erp_match.empty:
            erp_match = df_erp[df_erp['clean_gu_code'] == clean_key]
            is_old_item = True
            
        if not erp_match.empty:
            if len(erp_match) > 1 and is_old_item:
                erp_match = erp_match.sort_values(by='No', ascending=False)
                remark = "⚠️ 구코드 중복 (최근 등록건 매칭)"
            else:
                if str(raw_code) != str(erp_match.iloc[0]['품명']) and not is_old_item:
                    remark = f"기호교정 매칭 ({raw_code} ➔ {erp_match.iloc[0]['품명']})"
                else:
                    remark = "정상 매칭" if not is_old_item else "구품목코드 매칭"
            
            erp_row = erp_match.iloc[0]
            erp_p_name = erp_row.get('품명', '')
            erp_gu_code = erp_row.get(gu_col_name, '')
            erp_customer = erp_row.get('거래처명', '') if pd.notna(erp_row.get('거래처명', '')) else ""
            erp_qty = erp_row.get('재고수량', 0)
            erp_price = erp_row.get('입고단가', 0) if pd.notna(erp_row.get('입고단가', 0)) else 0
            
            erp_major = erp_row.get('대분류', '')
            erp_mid = erp_row.get('중분류', '')
            erp_sub = erp_row.get('소분류', '')
            erp_spec = erp_row.get('적요(규격)', '')
            erp_in_date = parse_excel_date(erp_row.get('입고일', ''))
            
            # 💡 [정밀 수정] PyArrow 타입 에러를 차단하기 위한 보유개월수 문자열 안전 포맷팅
            raw_holding_m = erp_row.get(month_col_name)
            if pd.notna(raw_holding_m) and str(raw_holding_m).strip() != "":
                try:
                    erp_holding_m = str(int(float(raw_holding_m)))
                except:
                    erp_holding_m = str(raw_holding_m).strip()
            else:
                erp_holding_m = ""
        else:
            # ERP 미등록 처리 (품명 및 거래처 완전 빈칸)
            erp_p_name = ""
            erp_gu_code = ""
            erp_customer = ""
            erp_qty = 0
            erp_price = 0
            erp_major = erp_mid = erp_sub = erp_spec = erp_in_date = erp_holding_m = ""
            remark = "🚨 WBS 기안문서 누락 (ERP 미등록)"
            
        # 스마트 예외 판별
        if erp_qty == 0 and dw_cur_qty > 0:
            if erp_p_name != "":
                status = "📦 선결제 미출고 (출고 대기)"
                remark = f"업체 선결제 완료로 ERP 재고 선차감 (대원창고 {int(dw_cur_qty)}대 보관 중)"
            else:
                status = "🚨 WBS 미기안"
        elif dw_cur_qty < erp_qty:
            status = f"📊 부분출고 ({int(erp_qty - dw_cur_qty)}대 기출고)"
        elif dw_cur_qty == erp_qty:
            status = "✅ 정상 일치"
        else:
            if erp_p_name != "":
                status = "📦 선결제 미출고 (출고 대기)"
                remark = f"ERP 수량({int(erp_qty)}대)보다 실재고({int(dw_cur_qty)}대)가 많음 (선결제 추정)"
            else:
                status = f"⚠️ 실재고 초과 (+{int(dw_cur_qty - erp_qty)}대)"
                
        formatted_price = f"{int(erp_price):,}" if erp_price > 0 else ""
        
        results.append({
            "대분류": str(erp_major),
            "중분류": str(erp_mid),
            "소분류": str(erp_sub),
            "대원_상품코드": str(dw_item_code),
            "대원_상품명": str(dw_item_name),
            "대원_업체상품코드": str(raw_code),
            "대원_현재재고": int(dw_cur_qty),
            "대원_가용재고": int(dw_avail_qty),
            "ERP_품명": str(erp_p_name),
            "적요(규격)": str(erp_spec),
            "거래처명": str(erp_customer),
            "ERP_재고수량": int(erp_qty),
            "입고단가": formatted_price,
            "입고일": str(erp_in_date),
            "보유개월수": str(erp_holding_m), # 안전하게 문자열 보존
            
            "상태": status,
            "비고": remark,
            "실재고금액": int(dw_cur_qty * erp_price),
            "ERP_구품목코드": str(erp_gu_code)
        })
        
    df_result = pd.DataFrame(results)
    
    # ERP 미등록 행(WBS 누락)을 표의 맨 아래로 자동 정렬
    df_result['is_missing_erp'] = (df_result['ERP_품명'] == "") | (df_result['ERP_품명'].isna())
    df_result = df_result.sort_values(by='is_missing_erp', ascending=True).drop(columns=['is_missing_erp'])
    
    return df_result, os.path.basename(latest_daewon), os.path.basename(latest_erp)

# 데이터 가공 실행
df_inventory, d_file, e_file = load_local_reconciled_data()

# --- [웹 대시보드 화면 렌더링] ---
st.title("📦 DKCNS 실시간 재고 및 오더 검증 에이전트")
st.caption(f"📂 사용된 로컬 파일: 대원 SCM ({d_file}) | 사내 ERP ({e_file})")

# 탭 구조 설계
tab1, tab2 = st.tabs(["🔍 실시간 재고 상세 조회", "🤖 AI 챗봇에게 물어보기"])

with tab1:
    col1, col2 = st.columns(2)
    with col1:
        selected_status = st.multiselect("재고 진행상태 필터링", options=df_inventory["상태"].unique(), default=[])
    with col2:
        search_query = st.text_input("거래처명 또는 상품코드 검색")
        
    df_filtered = df_inventory.copy()
    if selected_status:
        df_filtered = df_filtered[df_filtered["상태"].isin(selected_status)]
    if search_query:
        df_filtered = df_filtered[
            df_filtered["거래처명"].str.contains(search_query, case=False, na=False) |
            df_filtered["대원_업체상품코드"].str.contains(search_query, case=False, na=False) |
            df_filtered["대원_상품명"].str.contains(search_query, case=False, na=False)
        ]
        
    st.dataframe(df_filtered, use_container_width=True, height=500)

with tab2:
    st.subheader("💬 실재고 관련 궁금한 점을 편하게 질문하세요")
    st.info("💡 참고: Gemini API Key가 app.py에 입력되어 있어 영업팀 챗봇이 활성화된 상태입니다.")
    
    user_question = st.chat_input("질문 예시: '선결제 미출고 품목은 총 몇 개야?', '대교 거래처 장비 현황은?'")
    
    if user_question:
        with st.chat_message("user"):
            st.write(user_question)
            
        with st.chat_message("assistant"):
            if not CLEAN_API_KEY or CLEAN_API_KEY.startswith("AIzaSy...") or len(CLEAN_API_KEY) < 20:
                st.warning("⚠️ 챗봇 기능 활성화를 위해 `app.py` 소스코드 13번째 줄에 복사하신 API Key를 먼저 입력해 주세요!")
            else:
                try:
                    # 정제 완료된 안전한 키로 구글 API 호출
                    genai.configure(api_key=CLEAN_API_KEY)
                    model = genai.GenerativeModel("gemini-1.5-flash")
                    
                    context_csv = df_inventory.to_csv(index=False)
                    
                    prompt = f"""
                    너는 Daekyo CNS 영업팀의 재고 및 오더 검증 전문가야.
                    아래 실시간 재고 대사 데이터(CSV)에 기반해서만 답변하고, 없는 이야기는 지어내지 마.
                    질문 이외의 개인정보나 인사말 등은 생략하고 필요한 핵심 정보만 명확히 대답해줘.
                    금액 단위는 세련되게 세 자리 콤마와 원화(원)로 표기해.

                    [재고 데이터]
                    {context_csv}

                    [질문]
                    {user_question}
                    """
                    response = model.generate_content(prompt)
                    st.markdown(response.text)
                except Exception as e:
                    st.error(f"AI 호출 중 에러 발생: {e}")
