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
    # 📊 데이터 로드 및 초기 정제 (try-except 평탄화)
    # -------------------------------------------------------------------------
    df_inventory = pd.DataFrame()
    df_summary = pd.DataFrame()
    data_loaded = False

    try:
        df_inventory, df_summary = load_cloud_sheet_data()
        data_loaded = True
    except Exception as general_err:
        st.error(f"❌ 데이터 로드 중 예상치 못한 치명적 에러가 발생했습니다: {general_err}")

    # 데이터가 정상 로드되었을 때만 후속 UI 렌더링 진행
    if data_loaded:
        kst_tz = pytz.timezone('Asia/Seoul')
        update_datetime = datetime.now(kst_tz).strftime('%Y-%m-%d %H:%M:%S KST')
        
        RAW_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
        CLEAN_API_KEY = RAW_API_KEY.strip() if RAW_API_KEY else ""

        # 📌 정렬 대상 14개 고정 컬럼 설정 (카테고리 최전방 배치)
        target_columns = [
            "대분류", "중분류", "소분류", "품목코드", "적요(규격)", 
            "창고_실재고수량", "대원_실재고수량", "매칭상태", 
            "대원_상품명", "대원_업체상품코드", "기안상태", 
            "ERP_품명", "ERP_거래처명", "공급가액"
        ]
        
        existing_cols = [col for col in target_columns if col in df_inventory.columns]
        df_inventory = df_inventory[existing_cols]

        # ERP 미등록 및 미기안 항목 공백 치환
        if "ERP_품명" in df_inventory.columns:
            df_inventory["ERP_품명"] = df_inventory["ERP_품명"].astype(str).replace(["nan", "None", "-"], "")
        if "ERP_거래처명" in df_inventory.columns:
            df_inventory["ERP_거래처명"] = df_inventory["ERP_거래처명"].astype(str).replace(["nan", "None", "-"], "")

        # 정렬 가중치 생성 (미등록/공백은 최하단 배치용 1, 정상은 0)
        sort_weights = []
        for idx, row in df_inventory.iterrows():
            erp_name = str(row.get("ERP_품명", "")).strip()
            client_name = str(row.get("ERP_거래처명", "")).strip()
            if erp_name == "" or client_name == "":
                sort_weights.append(1)
            else:
                sort_weights.append(0)
        
        df_inventory["_sort_weight"] = sort_weights
        
        # 가중치 ➔ 대분류 ➔ 중분류 순서로 정렬 적용 (미등록은 최하단 배치)
        df_inventory = df_inventory.sort_values(by=["_sort_weight", "대분류", "중분류"], ascending=[True, True, True])
        df_inventory = df_inventory.drop(columns=["_sort_weight"])

        # -------------------------------------------------------------------------
        # 📂 대/중/소분류 다중 필터 구현 (좌측 사이드바 복구)
        # -------------------------------------------------------------------------
        st.sidebar.title("🎛️ 재고 필터링")
        st.sidebar.caption("카테고리를 선택해 재고 목록을 좁혀보세요.")

        # 대분류 필터
        all_categories = sorted(df_inventory["대분류"].unique().tolist()) if "대분류" in df_inventory.columns else []
        selected_categories = st.sidebar.multiselect("대분류 선택", all_categories)

        # 중분류 필터 (선택한 대분류 내에서 유효한 중분류만 추출)
        if selected_categories:
            filtered_by_cat = df_inventory[df_inventory["대분류"].isin(selected_categories)]
        else:
            filtered_by_cat = df_inventory

        all_subcategories = sorted(filtered_by_cat["중분류"].unique().tolist()) if "중분류" in filtered_by_cat.columns else []
        selected_subcategories = st.sidebar.multiselect("중분류 선택", all_subcategories)

        # 소분류 필터 (선택한 중분류 내에서 유효한 소분류만 추출)
        if selected_subcategories:
            filtered_by_subcat = filtered_by_cat[filtered_by_cat["중분류"].isin(selected_subcategories)]
        else:
            filtered_by_subcat = filtered_by_cat

        all_detailcategories = sorted(filtered_by_subcat["소분류"].unique().tolist()) if "소분류" in filtered_by_subcat.columns else []
        selected_detailcategories = st.sidebar.multiselect("소분류 선택", all_detailcategories)

        # 필터 최종 적용
        df_filtered = filtered_by_subcat
        if selected_detailcategories:
            df_filtered = df_filtered[df_filtered["소분류"].isin(selected_detailcategories)]

        # -------------------------------------------------------------------------
        # 🖥️ 메인 UI 화면 구성
        # -------------------------------------------------------------------------
        st.title("실시간 재고조회 시스템")
        st.caption(f"동기화 시간: {update_datetime}")
        st.markdown("---")

        tab1, tab2 = st.tabs(["실시간 재고조회", "AI 재고조회 챗봇"])

        # [탭 1] 실시간 재고조회 (필터 적용 데이터 프레임 출력)
        with tab1:
            st.dataframe(df_filtered, use_container_width=True, hide_index=True)

        # [탭 2] AI 재고조회 챗봇
        with tab2:
            st.subheader("💬 실재고 관련 궁금한 점을 편하게 질문하세요")
            st.caption("💡 32G 메모리 재고, 거래처별 재고 등 실재고 수량을 자연어로 질의할 수 있습니다.")
            
            if "chat_messages" not in st.session_state:
                st.session_state["chat_messages"] = []

            for msg in st.session_state["chat_messages"]:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])
            
            # 입력폼 꼬임 해소용 5:1 그리드 열 구조
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
                            # 1. 고속 판다스 벡터 검색으로 프롬프트 데이터 추출 (경량화)
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

                            # 2. [무한 대기 박멸] 구글 REST API 다이렉트 호출 (타임아웃 10초 강제)
                            api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={CLEAN_API_KEY}"
                            headers = {"Content-Type": "application/json"}
                            payload = {
                                "contents": [
                                    {"parts": [{"text": prompt}]}
                                ]
                            }

                            resp = requests.post(api_url, headers=headers, json=payload, timeout=10)
                            status_placeholder.empty()

                            if resp.status_code == 200:
                                resp_data = resp.json()
                                candidates = resp_data.get("candidates", [])
                                if candidates and "content" in candidates[0]:
                                    parts = candidates[0]["content"].get("parts", [])
                                    answer_text = parts[0].get("text", "⚠️ 생성된 답변 내용이 비어 있습니다.")
                                else:
                                    answer_text = "⚠️ 조건에 일치하는 재고 품목을 찾을 수 없습니다."
                                    
                                st.markdown(answer_text)
                                st.session_state["chat_messages"].append({"role": "assistant", "content": answer_text})

                            elif resp.status_code == 429:
                                err_msg = "⚠️ 현재 구글 API 하루 무료 사용 한도(20회)를 모두 소진했습니다. 잠시 후 시도하거나 새 키를 등록해 주세요."
                                st.warning(err_msg)
                                st.session_state["chat_messages"].append({"role": "assistant", "content": err_msg})

                            else:
                                err_json = resp.text
                                err_msg = f"❌ API 응답 오류 (상태코드 {resp.status_code}): {err_json}"
                                st.error(err_msg)
                                st.session_state["chat_messages"].append({"role": "assistant", "content": err_msg})
                                
                        except requests.exceptions.Timeout:
                            status_placeholder.empty()
                            err_msg = "⚠️ 구글 서버 응답 시간(10초)이 초과되었습니다. 다시 한 번 질문하기를 눌러주세요."
                            st.warning(err_msg)
                            st.session_state["chat_messages"].append({"role": "assistant", "content": err_msg})

                        except Exception as e:
                            status_placeholder.empty()
                            err_msg = f"❌ AI 호출 중 오류 발생: {e}"
                            st.error(err_msg)
                            st.session_state["chat_messages"].append({"role": "assistant", "content": err_msg})
