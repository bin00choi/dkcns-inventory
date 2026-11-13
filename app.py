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
st.set_page_config(page_title="재고 조회", layout="wide")

# CSS 스타일 적용 (모바일 1라인 강제 유지, '러닝중' 숨김, 스크롤바 가시성 증대)
st.markdown("""
<style>
/* 1. 우측 상단 Running... 스피너 및 Streamlit 기본 스피너 강제 숨김 */
[data-testid="stStatusWidget"] {
    visibility: hidden;
    height: 0px !important;
    width: 0px !important;
    overflow: hidden;
}
.stSpinner {
    display: none !important;
}

/* 2. 가로, 세로 스크롤바 선명하게 강화 */
::-webkit-scrollbar {
    width: 12px !important;
    height: 12px !important;
    display: block !important;
}
::-webkit-scrollbar-thumb {
    background-color: #a0a0a0 !important;
    border-radius: 6px !important;
    border: 2px solid #ffffff !important;
}
::-webkit-scrollbar-thumb:hover {
    background-color: #707070 !important;
}
::-webkit-scrollbar-track {
    background: #f1f1f1 !important;
}

/* 3. 모바일 화면에서 대/중/소분류 다중 필터 한 줄 강제 배치 */
@media (max-width: 640px) {
    div[data-testid="stHorizontalBlock"] {
        flex-direction: row !important;
        flex-wrap: nowrap !important;
        gap: 6px !important;
    }
    div[data-testid="stHorizontalBlock"] > div {
        min-width: 0 !important;
        flex: 1 1 0% !important;
    }
    /* 셀렉트박스 폰트 및 패딩 최적화 */
    div[data-baseweb="select"] {
        font-size: 11px !important;
    }
}
</style>
""", unsafe_allow_html=True)

# -------------------------------------------------------------------------
# 🔑 KST 기준 오늘 날짜 8자리(YYYYMMDD) 패스워드 입력 게이트웨이
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

    st.markdown("### 🔑 패스워드 입력")
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
    # ☁️ 구글 스프레드시트 및 Gemini API 연동 설정
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
        
        # 구글시트 실제 최종 변경일시 획득
        sheet_updated_raw = "알 수 없음"
        try:
            sheet_updated_raw = spreadsheet.updated
        except Exception:
            pass
            
        sheet_realtime = spreadsheet.worksheet("실시간재고현황")
        data_realtime = sheet_realtime.get_all_records()
        df_inv = pd.DataFrame(data_realtime)
        
        sheet_summary = spreadsheet.worksheet("재고검증요약")
        data_summary = sheet_summary.get_all_records()
        df_sum = pd.DataFrame(data_summary)
        
        return df_inv, df_sum, sheet_updated_raw

    # -------------------------------------------------------------------------
    # 🛠️ 데이터 로드 및 초기 정제 (try-except 구조 평탄화)
    # -------------------------------------------------------------------------
    df_inventory = pd.DataFrame()
    df_summary = pd.DataFrame()
    sheet_updated_time = ""
    data_loaded = False
    
    try:
        df_inventory, df_summary, sheet_updated_time = load_cloud_sheet_data()
        data_loaded = True
    except Exception as general_err:
        st.error(f"❌ 데이터 로드 중 예상치 못한 치명적 에러가 발생했습니다: {general_err}")

    # 데이터가 정상 로드되었을 때만 후속 로직 진행
    if data_loaded:
        # 구글시트 업데이트 시간 KST 포맷팅
        try:
            parsed_time = pd.to_datetime(sheet_updated_time).tz_convert('Asia/Seoul')
            update_datetime = parsed_time.strftime('%Y-%m-%d %H:%M:%S KST')
        except Exception:
            kst_tz = pytz.timezone('Asia/Seoul')
            update_datetime = datetime.now(kst_tz).strftime('%Y-%m-%d %H:%M:%S KST')
        
        RAW_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
        CLEAN_API_KEY = RAW_API_KEY.strip() if RAW_API_KEY else ""

        # ERP 미등록 및 WBS 미기안 항목 빈칸("") 치환
        unregistered_keywords = ["nan", "None", "-", "미등록", "ERP미등록", "ERP 미등록", "미기안", "WBS미기안", "WBS 미기안"]
        
        for col in ["대분류", "중분류", "소분류", "ERP_품명", "ERP_거래처명"]:
            if col in df_inventory.columns:
                df_inventory[col] = df_inventory[col].astype(str).str.strip()
                df_inventory[col] = df_inventory[col].replace(unregistered_keywords, "")
                if col in ["ERP_품명", "ERP_거래처명"]:
                    mask_unreg = df_inventory[col].str.contains("미등록|미기안", case=False, na=False)
                    df_inventory.loc[mask_unreg, col] = ""

        # 최하단 배치를 위한 정렬 가중치 계산 (정상=0, 미등록/빈칸=1)
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
        # 📋 컬럼명 유연 매칭 및 "입고일(Invoice)" 동적 이름 지정
        # -------------------------------------------------------------------------
        # 11개 요청 컬럼과 스프레드시트 내 컬럼 매핑
        target_mappings = {
            "대원_업체상품코드": ["대원_업체상품코드"],
            "대원_상품명": ["대원_상품명"],
            "대원_현재재고": ["대원_현재재고", "대원_실재고수량"],
            "대원_가용재고": ["대원_가용재고"],
            "ERP_품명": ["ERP_품명"],
            "적요(규격)": ["적요(규격)"],
            "거래처명": ["거래처명", "ERP_거래처명"],
            "ERP_재고수량": ["ERP_재고수량", "창고_실재고수량"],
            "입고단가": ["입고단가", "공급가액"],
            "입고일(Invoice)": ["입고일(Invoice)", "입고일"],
            "보유개월수": ["보유개월수"]
        }

        # 원본 데이터프레임의 컬럼 이름을 유연하게 매칭하여 이름 변경
        rename_dict = {}
        for target_name, alternatives in target_mappings.items():
            for alt in alternatives:
                if alt in df_inventory.columns:
                    rename_dict[alt] = target_name
                    break
        
        df_inventory = df_inventory.rename(columns=rename_dict)

        # -------------------------------------------------------------------------
        # 🖥️ 메인 UI 화면 구성
        # -------------------------------------------------------------------------
        st.title("🔍 재고 조회")
        st.caption(f"마지막 업데이트: {update_datetime}")
        st.markdown("---")
        
        tab1, tab2 = st.tabs(["📋 실시간 재고 리스트", "💬 AI 챗봇에게 물어보기"])
        
        with tab1:
            # 1. 원래 존재하던 통합 검색창 복구
            search_query = st.text_input("🔍 품목명 / 코드 등 검색", "", placeholder="원하시는 검색어를 입력하시면 목록이 필터링됩니다.")
            
            # 2. 대분류 / 중분류 / 소분류 필터 가로 배치 (모바일 1라인 강제 유지 CSS가 적용됨)
            f_col1, f_col2, f_col3 = st.columns(3)
            
            with f_col1:
                cat1_list = sorted([x for x in df_inventory["대분류"].unique().tolist() if x != ""]) if "대분류" in df_inventory.columns else []
                if "대분류" in df_inventory.columns and "" in df_inventory["대분류"].values:
                    cat1_list.append("미분류(빈칸)")
                sel_cat1 = st.multiselect("대분류", cat1_list, placeholder="전체")
                
            df_step1 = df_inventory.copy()
            if "대분류" in df_step1.columns and sel_cat1:
                actual_cat1 = ["" if x == "미분류(빈칸)" else x for x in sel_cat1]
                df_step1 = df_step1[df_step1["대분류"].isin(actual_cat1)]
                
            with f_col2:
                cat2_list = sorted([x for x in df_step1["중분류"].unique().tolist() if x != ""]) if "중분류" in df_step1.columns else []
                if "중분류" in df_step1.columns and "" in df_step1["중분류"].values:
                    cat2_list.append("미분류(빈칸)")
                sel_cat2 = st.multiselect("중분류", cat2_list, placeholder="전체")
                
            df_step2 = df_step1.copy()
            if "중분류" in df_step2.columns and sel_cat2:
                actual_cat2 = ["" if x == "미분류(빈칸)" else x for x in sel_cat2]
                df_step2 = df_step2[df_step2["중분류"].isin(actual_cat2)]
                
            with f_col3:
                cat3_list = sorted([x for x in df_step2["소분류"].unique().tolist() if x != ""]) if "소분류" in df_step2.columns else []
                if "소분류" in df_step2.columns and "" in df_step2["소분류"].values:
                    cat3_list.append("미분류(빈칸)")
                sel_cat3 = st.multiselect("소분류", cat3_list, placeholder="전체")
                
            df_filtered = df_step2.copy()
            if "소분류" in df_filtered.columns and sel_cat3:
                actual_cat3 = ["" if x == "미분류(빈칸)" else x for x in sel_cat3]
                df_filtered = df_filtered[df_filtered["소분류"].isin(actual_cat3)]
                
            # 3. 통합 검색창 입력 시 전체 컬럼 대상 필터링 수행
            if search_query.strip():
                search_mask = pd.Series(False, index=df_filtered.index)
                for col in df_filtered.columns:
                    search_mask = search_mask | df_filtered[col].astype(str).str.contains(search_query.strip(), case=False, na=False)
                df_filtered = df_filtered[search_mask]

            # 4. 순번 넘버링 추가 및 요청된 11개 핵심 컬럼 고정 순서 정렬
            fixed_columns = [
                "대원_업체상품코드", "대원_상품명", "대원_현재재고", "대원_가용재고", 
                "ERP_품명", "적요(규격)", "거래처명", "ERP_재고수량", 
                "입고단가", "입고일(Invoice)", "보유개월수"
            ]
            
            # 실제 존재 컬럼 및 부재 시 보완
            for col in fixed_columns:
                if col not in df_filtered.columns:
                    df_filtered[col] = ""
            
            # 나머지 추가 컬럼 필터링
            remaining_cols = [col for col in df_filtered.columns if col not in fixed_columns]
            
            # 최종 정렬 컬럼 세트 생성
            df_ordered = df_filtered[fixed_columns + remaining_cols].copy()
            
            # -------------------------------------------------------------------------
            # 💲 데이터 정밀 포맷팅 (입고단가 콤마 추가, 입고일(Invoice) yyyy-mm-dd 강제)
            # -------------------------------------------------------------------------
            # 입고단가 콤마(,) 포맷팅 함수
            def format_price(val):
                if pd.isna(val) or str(val).strip() in ["", "nan", "None"]:
                    return ""
                try:
                    num = float(str(val).replace(",", "").strip())
                    return f"{int(num):,}"
                except ValueError:
                    return val

            # 입고일 YYYY-MM-DD 형식 포맷팅 함수
            def format_date(val):
                if pd.isna(val) or str(val).strip() in ["", "nan", "None"]:
                    return ""
                try:
                    # pandas로 파싱 시도
                    dt_parsed = pd.to_datetime(val)
                    return dt_parsed.strftime('%Y-%m-%d')
                except Exception:
                    # 파싱 실패 시 공백 기준 앞의 부분만 추출하여 간소화
                    return str(val).split(" ")[0].strip()

            df_ordered["입고단가"] = df_ordered["입고단가"].apply(format_price)
            df_ordered["입고일(Invoice)"] = df_ordered["입고일(Invoice)"].apply(format_date)
            
            # '순번' 컬럼을 가장 맨 앞에 생성 및 삽입
            df_ordered = df_ordered.reset_index(drop=True)
            df_ordered.insert(0, "순번", range(1, len(df_ordered) + 1))
            
            # 최종 데이터 프레임 표출
            st.dataframe(df_ordered, use_container_width=True, hide_index=True)

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
                        status_placeholder.info("🔍 실재고 데이터를 검색하고 있습니다...")
                        
                        try:
                            # 1. 고속 판다스 벡터 검색으로 프롬프트 데이터 추출 (경량화)
                            ai_context_cols = [c for c in df_inventory.columns if c not in ["_sort_weight", "_cat1", "_cat2", "_cat3"]]
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
                                st.session_state["chat_
