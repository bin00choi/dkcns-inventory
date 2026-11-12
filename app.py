import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import google.generativeai as genai
from datetime import datetime
import pytz
import requests
import json

# Page 기본 설정
st.set_page_config(page_title="실시간 재고조회 시스템", layout="wide")

# -------------------------------------------------------------------------
# 🔐 KST 기준 오늘 날짜 8자리(YYYYMMDD) 로그인 게이트웨이
# -------------------------------------------------------------------------
def check_password():
    kst = pytz.timezone('Asia/Seoul')
    correct_password = datetime.now(kst).strftime('%Y%m%d')

    def password_entered():
        user_input = st.session_state.get("password_input", "")
        if user_input == correct_password:
            st.session_state["password_correct"] = True
            if "password_input" in st.session_state:
                del st.session_state["password_input"]
        else:
            st.session_state["password_correct"] = False

    if st.session_state.get("password_correct", False):
        return True

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
    # ⚙️ 구글 스프레드시트 연동 설정
    # -------------------------------------------------------------------------
    @st.cache_data(ttl=60)
    def load_cloud_sheet_data():
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        
        creds_dict = dict(st.secrets["gcp_service_account"])
        
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
            
        creds_dict["token_uri"] = "https://oauth2.googleapis.com/token"
        
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        
        spreadsheet = client.open("dkcns_inventory")
        
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
    df_inventory = pd.DataFrame()
    df_summary = pd.DataFrame()
    data_loaded = False

    try:
        df_inventory, df_summary = load_cloud_sheet_data()
        data_loaded = True
    except Exception as general_err:
        st.error(f"❌ 데이터 로드 중 에러가 발생했습니다: {general_err}")

    if data_loaded:
        kst_tz = pytz.timezone('Asia/Seoul')
        update_datetime = datetime.now(kst_tz).strftime('%Y-%m-%d %H:%M:%S')
        
        RAW_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
        CLEAN_API_KEY = RAW_API_KEY.strip() if RAW_API_KEY else ""

        # 📌 14개 고정 컬럼 설정 (대/중/소분류 최전방 배치)
        target_columns = [
            "대분류", "중분류", "소분류", "품목코드", "적요(규격)", 
            "창고_실재고수량", "대원_실재고수량", "매칭상태", 
            "대원_상품명", "대원_업체상품코드", "기안상태", 
            "ERP_품명", "ERP_거래처명", "공급가액"
        ]
        
        existing_cols = [col for col in target_columns if col in df_inventory.columns]
        df_inventory = df_inventory[existing_cols].copy()

        # 📌 ERP 미등록 및 WBS 미기안 항목 빈칸("") 치환 및 최하단 정렬 처리
        unregistered_keywords = ["nan", "None", "-", "미등록", "ERP미등록", "ERP 미등록", "미기안", "WBS미기안", "WBS 미기안"]
        
        for col in ["대분류", "중분류", "소분류", "ERP_품명", "ERP_거래처명"]:
            if col in df_inventory.columns:
                df_inventory[col] = df_inventory[col].astype(str).str.strip()
                df_inventory[col] = df_inventory[col].replace(unregistered_keywords, "")
                # 부분 문자열로 '미등록'이나 '미기안'이 들어간 경우에도 빈칸 처리 (ERP_품명, ERP_거래처명)
                if col in ["ERP_품명", "ERP_거래처명"]:
                    mask_unreg = df_inventory[col].str.contains("미등록|미기안", case=False, na=False)
                    df_inventory.loc[mask_unreg, col] = ""

        # 📌 최하단 배치를 위한 정렬 가중치 계산 (정상=0, 미등록/빈칸=1)
        def get_sort_weight(row):
            erp_name = str(row.get("ERP_품명", "")).strip()
            client_name = str(row.get("ERP_거래처명", "")).strip()
            cat_main = str(row.get("대분류", "")).strip()
            match_status = str(row.get("매칭상태", "")).strip()
            draft_status = str(row.get("기안상태", "")).strip()
            
            if (erp_name == "" or client_name == "" or cat_main == "" or 
                "미등록" in match_status or "미기안" in draft_status):
                return 1
            return 0

        df_inventory["_sort_weight"] = df_inventory.apply(get_sort_weight, axis=1)
        
        # 대/중/소분류 빈칸도 정상 항목 내에서는 아래로 가도록 보조 정렬키 생성
        df_inventory["_cat1"] = df_inventory["대분류"].apply(lambda x: "ㅎㅎㅎ" if x == "" else x)
        df_inventory["_cat2"] = df_inventory["중분류"].apply(lambda x: "ㅎㅎㅎ" if x == "" else x)
        df_inventory["_cat3"] = df_inventory["소분류"].apply(lambda x: "ㅎㅎㅎ" if x == "" else x)

        df_inventory = df_inventory.sort_values(
            by=["_sort_weight", "_cat1", "_cat2", "_cat3"],
            ascending=[True, True, True, True]
        ).drop(columns=["_sort_weight", "_cat1", "_cat2", "_cat3"]).reset_index(drop=True)

        # -------------------------------------------------------------------------
        # 🖥️ 메인 타이틀 및 탭 구성
        # -------------------------------------------------------------------------
        st.title("📦 실시간 재고조회 시스템")
        st.caption(f"마지막 업데이트: {update_datetime}")

        tab1, tab2 = st.tabs(["📊 실시간 재고 리스트", "🤖 AI 챗봇에게 물어보기"])

        # -------------------------------------------------------------------------
        # [탭 1] 📊 실시간 재고 리스트 (메인 화면 상단 대/중/소분류 필터 복구)
        # -------------------------------------------------------------------------
        with tab1:
            # 사이드바가 아닌 탭 1 상단 가로 3열 배치
            f_col1, f_col2, f_col3 = st.columns(3)
            
            with f_col1:
                cat1_list = sorted([x for x in df_inventory["대분류"].unique().tolist() if x != ""])
                if "" in df_inventory["대분류"].values:
                    cat1_list.append("미분류(빈칸)")
                sel_cat1 = st.multiselect("대분류", cat1_list, placeholder="전체")
                
            # 대분류 필터 반영
            df_step1 = df_inventory.copy()
            if sel_cat1:
                actual_cat1 = ["" if x == "미분류(빈칸)" else x for x in sel_cat1]
                df_step1 = df_step1[df_step1["대분류"].isin(actual_cat1)]
                
            with f_col2:
                cat2_list = sorted([x for x in df_step1["중분류"].unique().tolist() if x != ""])
                if "" in df_step1["중분류"].values:
                    cat2_list.append("미분류(빈칸)")
                sel_cat2 = st.multiselect("중분류", cat2_list, placeholder="전체")
                
            # 중분류 필터 반영
            df_step2 = df_step1.copy()
            if sel_cat2:
                actual_cat2 = ["" if x == "미분류(빈칸)" else x for x in sel_cat2]
                df_step2 = df_step2[df_step2["중분류"].isin(actual_cat2)]
                
            with f_col3:
                cat3_list = sorted([x for x in df_step2["소분류"].unique().tolist() if x != ""])
                if "" in df_step2["소분류"].values:
                    cat3_list.append("미분류(빈칸)")
                sel_cat3 = st.multiselect("소분류", cat3_list, placeholder="전체")
                
            # 소분류 필터 반영
            df_filtered = df_step2.copy()
            if sel_cat3:
                actual_cat3 = ["" if x == "미분류(빈칸)" else x for x in sel_cat3]
                df_filtered = df_filtered[df_filtered["소분류"].isin(actual_cat3)]

            st.dataframe(df_filtered, use_container_width=True, hide_index=True)

        # -------------------------------------------------------------------------
        # [탭 2] 🤖 AI 챗봇에게 물어보기
        # -------------------------------------------------------------------------
        with tab2:
            st.subheader("💬 실재고 관련 궁금한 점을 편하게 질문하세요")
            st.caption("💡 32G 메모리 재고, 거래처별 재고 등 실재고 수량을 자연어로 질의할 수 있습니다.")
            
            if "chat_messages" not in st.session_state:
                st.session_state["chat_messages"] = []

            for msg in st.session_state["chat_messages"]:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])
            
            col_input, col_btn = st.columns((5, 1))
            with col_input:
                user_question = st.text_input(
                    "질문 입력",
                    placeholder="예: '32기가 메모리 재고 있어?', '하루제스트 재고 목록?'",
                    label_visibility="collapsed",
                    key="chat_input_text"
                )
            with col_btn:
                ask_clicked = st.button("질문하기", use_container_width=True, type="primary")
            
            if ask_clicked and user_question.strip():
                st.session_state["chat_messages"].append({"role": "user", "content": user_question})
                with st.chat_message("user"):
                    st.markdown(user_question)
                    
                with st.chat_message("assistant"):
                    if not CLEAN_API_KEY or CLEAN_API_KEY.startswith("AIzaSy...") or len(CLEAN_API_KEY) < 20:
                        warn_msg = "⚠️ 챗봇 기능 활성화를 위해 Streamlit Secrets에 올바른 Gemini API Key를 등록해 주세요."
                        st.warning(warn_msg)
                        st.session_state["chat_messages"].append({"role": "assistant", "content": warn_msg})
                    else:
                        status_placeholder = st.empty()
                        status_placeholder.info("⚡ 실재고 데이터를 검색하고 있습니다...")
                        
                        try:
                            ai_context_cols = [c for c in target_columns if c in df_inventory.columns]
                            search_keywords = [w.strip().lower() for w in user_question.split() if len(w.strip()) >= 2]
                            matched_df = df_inventory.copy()
                            
                            if search_keywords:
                                mask = pd.Series(False, index=matched_df.index)
                                for kw in search_keywords:
                                    for col in ai_context_cols:
                                        mask = mask | matched_df[col].astype(str).str.contains(kw, case=False, na=False)
                                filtered_df = matched_df[mask]
                                sample_df = filtered_df.head(30) if len(filtered_df) > 0 else matched_df.head(30)
                            else:
                                sample_df = matched_df.head(30)
                                
                            context_csv = sample_df[ai_context_cols].to_csv(index=False)
                            
                            prompt = f"""너는 실시간 재고 관리 시스템의 오더 검증 전문가야.
아래 제공된 실재고 데이터(CSV)만을 기반으로 사실에 입각하여 답변해.
질문과 관련된 품목의 [품명], [규격/적요], [대원_실재고수량], [창고_실재고수량], [공급가액] 정보를 깔끔한 표(Markdown Table) 형식으로 정리해줘.
절대 없는 수량이나 품목을 지어내지 말고, 데이터에 없으면 '해당 조건에 맞는 실재고가 확인되지 않습니다'라고 명확히 안내해.

[업데이트 일시]: {update_datetime}
[창고 연동 실재고 데이터]
{context_csv}

[질문]
{user_question}"""

                            api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={CLEAN_API_KEY}"
                            headers = {"Content-Type": "application/json"}
                            payload = {
                                "contents": [
                                    {"parts": [{"text": prompt}]}
                                
