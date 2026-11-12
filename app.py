import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import google.generativeai as genai
from datetime import datetime
import pytz

# Page 기본 설정
st.set_page_config(page_title="실시간 재고조회 시스템", layout="wide")

# -------------------------------------------------------------------------
# 🔐 KST 기준 오늘 날짜 8자리(YYYYMMDD) 로그인 게이트웨이 (KeyError 해결)
# -------------------------------------------------------------------------
def check_password():
    kst = pytz.timezone('Asia/Seoul')
    correct_password = datetime.now(kst).strftime('%Y%m%d')

    def password_entered():
        # .get() 메서드를 사용하여 세션 키 미생성 시 발생하는 KeyError 원천 차단
        user_input = st.session_state.get("password_input", "")
        if user_input == correct_password:
            st.session_state["password_correct"] = True
            if "password_input" in st.session_state:
                del st.session_state["password_input"]
        else:
            st.session_state["password_correct"] = False

    if st.session_state.get("password_correct", False):
        return True

    # 로그인 UI 렌더링 (사명 제거 및 익명화 정책 준수)
    st.markdown("### 🔐 시스템 접속 인증")
    st.text_input(
        "비밀번호를 입력하세요",
        type="password",
        on_change=password_entered,
        key="password_input"
    )

    if "password_correct" in st.session_state and not st.session_state["password_correct"]:
        st.error("비밀번호가 올바르지 않습니다. 관리자에게 문의해 주세요.")

    return False

# 비밀번호 통과 시에만 메인 프로그램 실행
if check_password():

    # -------------------------------------------------------------------------
    # ⚙️ 구글 스프레드시트 및 Gemini API 연동 설정
    # -------------------------------------------------------------------------
    @st.cache_data(ttl=60)
    def load_cloud_sheet_data():
        """구글 시트 연동 및 데이터 수집"""
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        
        # Streamlit Secrets 로드
        creds_dict = dict(st.secrets["gcp_service_account"])
        
        # 비공개 키 개행 문자 역슬래시 처리 대응
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
            
        # DNS 해상 오류 방지를 위한 토큰 URI 주입
        creds_dict["token_uri"] = "https://oauth2.googleapis.com/token"
        
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        
        # 구글 스프레드시트 이름 열기
        spreadsheet = client.open("dkcns_inventory")
        
        # 시트 데이터 파싱
        sheet_realtime = spreadsheet.worksheet("실시간재고현황")
        data_realtime = sheet_realtime.get_all_records()
        df_inv = pd.DataFrame(data_realtime)
        
        sheet_summary = spreadsheet.worksheet("재고검증요약")
        data_summary = sheet_summary.get_all_records()
        df_sum = pd.DataFrame(data_summary)
        
        return df_inv, df_sum

    # -------------------------------------------------------------------------
    # 📊 데이터 로드 및 초기 정제
    # -------------------------------------------------------------------------
    try:
        df_inventory, df_summary = load_cloud_sheet_data()
        
        # KST 업데이트 시간 기록
        kst_tz = pytz.timezone('Asia/Seoul')
        update_datetime = datetime.now(kst_tz).strftime('%Y-%m-%d %H:%M:%S KST')
        
        # API 키 로드 및 공백 제거
        RAW_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
        CLEAN_API_KEY = RAW_API_KEY.strip() if RAW_API_KEY else ""

        # -------------------------------------------------------------------------
        # 📌 정렬 로직 및 미등록 항목 최하단 배치 처리
        # -------------------------------------------------------------------------
        # 14개 주요 정렬 타겟 컬럼 지정 (카테고리 최전방 배치)
        target_columns = [
            "대분류", "중분류", "소분류", "품목코드", "적요(규격)", 
            "창고_실재고수량", "대원_실재고수량", "매칭상태", 
            "대원_상품명", "대원_업체상품코드", "기안상태", 
            "ERP_품명", "ERP_거래처명", "공급가액"
        ]
        
        # 미존재 컬럼 대비 안전 필터링
        existing_cols = [col for col in target_columns if col in df_inventory.columns]
        df_inventory = df_inventory[existing_cols]

        # ERP 미등록 및 기안 미확정 공백 처리 및 가중치 계산
        if "ERP_품명" in df_inventory.columns:
            df_inventory["ERP_품명"] = df_inventory["ERP_품명"].astype(str).replace(["nan", "None", "-"], "")
        if "ERP_거래처명" in df_inventory.columns:
            df_inventory["ERP_거래처명"] = df_inventory["ERP_거래처명"].astype(str).replace(["nan", "None", "-"], "")

        # 정렬 가중치 생성 (미등록/공백 항목은 최하단 배치용 1, 정상은 0)
        sort_weights = []
        for idx, row in df_inventory.iterrows():
            erp_name = str(row.get("ERP_품명", "")).strip()
            client_name = str(row.get("ERP_거래처명", "")).strip()
            if erp_name == "" or client_name == "":
                sort_weights.append(1)
            else:
                sort_weights.append(0)
        
        df_inventory["_sort_weight"] = sort_weights
        
        # 가중치 우선 정렬 후 가중치 열 임시 삭제
        df_inventory = df_inventory.sort_values(by=["_sort_weight", "대분류", "중분류"], ascending=[True, True, True])
        df_inventory = df_inventory.drop(columns=["_sort_weight"])

        # -------------------------------------------------------------------------
        # 🖥️ 원래의 깔끔한 메인 UI 화면 구성 (원상복구 완료)
        # -------------------------------------------------------------------------
        st.title("실시간 재고조회 시스템")  # 원래 타이틀 복구
        st.caption(f"동기화 시간: {update_datetime}")  # 원래 디자인 복구
        st.markdown("---")

        # -------------------------------------------------------------------------
        # 🗂️ 탭 레이아웃 구성
        # -------------------------------------------------------------------------
        tab1, tab2 = st.tabs(["실시간 재고조회", "AI 재고조회 챗봇"])  # 원래 탭 이름 복구

        # [탭 1] 실시간 재고조회
        with tab1:
            st.dataframe(df_inventory, use_container_width=True, hide_index=True)

        # [탭 2] AI 재고조회 챗봇 (안정적인 Form 전송 및 최신 gemini-3.8-flash 적용)
        with tab2:
            st.subheader("💬 실재고 관련 궁금한 점을 편하게 질문하세요")
            st.caption("💡 32G 메모리 재고, 거래처별 재고 등 실재고 수량을 자연어로 질의할 수 있습니다.")
            
            # 대화 세션 상태 초기화
            if "chat_messages" not in st.session_state:
                st.session_state["chat_messages"] = []

            # 이전 대화 렌더링
            for msg in st.session_state["chat_messages"]:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])
            
            # 탭 내 충돌 방지를 위한 안정적인 st.form 입력 방식 (2분할 지정)
            with st.form(key="chat_form", clear_on_submit=True):
                col_input, col_btn = st.columns(2)
                with col_input:
                    user_question = st.text_input(
                        "질문 입력",
                        placeholder="예: '32기가 메모리 재고 있어?', '하루제스트 재고 목록?'",
                        label_visibility="collapsed"
                    )
                with col_btn:
                    submit_btn = st.form_submit_button("질문하기", use_container_width=True)
            
            # 전송 제출 시 메인 로직 가동
            if submit_btn and user_question.strip():
                st.session_state["chat_messages"].append({"role": "user", "content": user_question})
                with st.chat_message("user"):
                    st.markdown(user_question)
                    
                with st.chat_message("assistant"):
                    if not CLEAN_API_KEY or CLEAN_API_KEY.startswith("AIzaSy...") or len(CLEAN_API_KEY) < 20:
                        warn_msg = "⚠️ 챗봇 기능 활성화를 위해 Streamlit Secrets에 올바른 Gemini API Key를 등록해 주세요."
                        st.warning(warn_msg)
                        st.session_state["chat_messages"].append({"role": "assistant", "content": warn_msg})
                    else:
                        with st.spinner("🔍 실재고 데이터를 분석하여 답변을 작성하고 있습니다..."):
                            try:
                                # transport='rest' 옵션을 명시하여 gRPC 포트 막힘으로 인한 504 무한 대기 원천 차단
                                genai.configure(api_key=CLEAN_API_KEY, transport='rest')
                                
                                # [★교체 완료] 404 에러를 유발하던 구형 1.5 모델 대신 최신 'gemini-3.8-flash' 모델 적용
                                model = genai.GenerativeModel("gemini-3.8-flash")
                                
                                # 대용량 질문 용량 초과 방지: 관련 있는 최상위 30개 행만 필터링 후 전송
                                ai_context_cols = [c for c in target_columns if c in df_inventory.columns]
                                
                                if len(df_inventory) > 30:
                                    keywords = [w.strip().lower() for w in user_question.split() if len(w.strip()) >= 2]
                                    if keywords:
                                        row_scores = []
                                        for idx, row in df_inventory.iterrows():
                                            row_text = " ".join(row.astype(str).values).lower()
                                            score = sum(1 for kw in keywords if kw in row_text)
                                            row_scores.append((score, idx))
                                        row_scores.sort(key=lambda x: x[0], reverse=True)
                                        best_indices = [idx for score, idx in row_scores[:30]]
                                        sample_df = df_inventory.loc[best_indices]
      
