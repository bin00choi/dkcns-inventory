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
    # ⚙️ 구글 스프레드시트 및 Gemini API 연동 설정
    # -------------------------------------------------------------------------
    @st.cache_data(ttl=60)
    def load_cloud_sheet_data():
        """구글 시트 연동 및 데이터 수집"""
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
    try:
        df_inventory, df_summary = load_cloud_sheet_data()
        
        kst_tz = pytz.timezone('Asia/Seoul')
        update_datetime = datetime.now(kst_tz).strftime('%Y-%m-%d %H:%M:%S KST')
        
        RAW_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
        CLEAN_API_KEY = RAW_API_KEY.strip() if RAW_API_KEY else ""

        # 📌 정렬 로직 및 미등록 항목 최하단 배치 처리
        target_columns = [
            "대분류", "중분류", "소분류", "품목코드", "적요(규격)", 
            "창고_실재고수량", "대원_실재고수량", "매칭상태", 
            "대원_상품명", "대원_업체상품코드", "기안상태", 
            "ERP_품명", "ERP_거래처명", "공급가액"
        ]
        
        existing_cols = [col for col in target_columns if col in df_inventory.columns]
        df_inventory = df_inventory[existing_cols]

        if "ERP_품명" in df_inventory.columns:
            df_inventory["ERP_품명"] = df_inventory["ERP_품명"].astype(str).replace(["nan", "None", "-"], "")
        if "ERP_거래처명" in df_inventory.columns:
            df_inventory["ERP_거래처명"] = df_inventory["ERP_거래처명"].astype(str).replace(["nan", "None", "-"], "")

        sort_weights = []
        for idx, row in df_inventory.iterrows():
            erp_name = str(row.get("ERP_품명", "")).strip()
            client_name = str(row.get("ERP_거래처명", "")).strip()
            if erp_name == "" or client_name == "":
                sort_weights.append(1)
            else:
                sort_weights.append(0)
        
        df_inventory["_sort_weight"] = sort_weights
        df_inventory = df_inventory.sort_values(by=["_sort_weight", "대분류", "중분류"], ascending=[True, True, True])
        df_inventory = df_inventory.drop(columns=["_sort_weight"])

        # 원래의 깔끔한 메인 UI 화면 구성 (원상복구)
        st.title("실시간 재고조회 시스템")
        st.caption(f"동기화 시간: {update_datetime}")
        st.markdown("---")

        tab1, tab2 = st.tabs(["실시간 재고조회", "AI 재고조회 챗봇"])

        with tab1:
            st.dataframe(df_inventory, use_container_width=True, hide_index=True)

        with tab2:
            st.subheader("💬 실재고 관련 궁금한 점을 편하게 질문하세요")
            st.caption("💡 32G 메모리 재고, 거래처별 재고 등 실재고 수량을 자연어로 질의할 수 있습니다.")
            
            if "chat_messages" not in st.session_state:
                st.session_state["chat_messages"] = []

            for msg in st.session_state["chat_messages"]:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])
            
            # 락을 원천 차단하는 가벼운 개별 입력 구조 (st.form 제거)
            col_input, col_btn = st.columns()
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
                        status_placeholder.info("⚡ 실재고 데이터를 분석하여 답변을 작성하고 있습니다...")
                        
                        try:
                            genai.configure(api_key=CLEAN_API_KEY, transport='rest')
                            model = genai.GenerativeModel("gemini-3.8-flash")
                            
                            ai_context_cols = [c for c in target_columns if c in df_inventory.columns]
                            
                            # 고속 판다스 벡터 검색으로 프롬프트 데이터 추출
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

                            response = model.generate_content(prompt)
                            answer_text = response.text if hasattr(response, "text") and response.text else "⚠️ 답변을 생성하지 못했습니다. 질문을 구체화해 주세요."
                            
                            status_placeholder.empty()
                            st.markdown(answer_text)
                            st.session_state["chat_messages"].append({"role": "assistant", "content": answer_text})
                            
                        except Exception as e:
                            status_placeholder.empty()
                            err_msg = f"❌ AI 호출 중 에러 발생: {e}"
                            st.error(err_msg)
                            st.session_state["chat_messages"].append({"role": "assistant", "content": err_msg})

    except Exception as general_err:
        st.error(f"❌ 데이터 로드 중 예상치 못한 치명적 에러가 발생했습니다: {general_err}")
