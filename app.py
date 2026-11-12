import os
import datetime
import pytz
import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import google.generativeai as genai

# =========================================================================
# [설정] 1. 웹 대시보드 환경 설정 (스마트폰 모바일 뷰 최적화)
# =========================================================================
st.set_page_config(
    page_title="재고조회",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# =========================================================================
# [보안] 2. 인증 키 안전 로드 (Streamlit Secrets)
# =========================================================================
try:
    RAW_GEMINI_KEY = st.secrets["GEMINI_API_KEY"]
except Exception:
    RAW_GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")

CLEAN_API_KEY = str(RAW_GEMINI_KEY).strip().replace("'", "").replace('"', "")

try:
    GCP_INFO = dict(st.secrets["gcp_service_account"])
except Exception:
    GCP_INFO = None

# =========================================================================
# [보안 로그인] 3. 오늘 날짜 8자리 패스코드 가드
# =========================================================================
if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

def get_today_password():
    """한국 시간(KST) 기준으로 오늘 날짜 8자리 문자열(YYYYMMDD)을 반환합니다."""
    kst = pytz.timezone("Asia/Seoul")
    today_kst = datetime.datetime.now(kst)
    return today_kst.strftime("%Y%m%d")

def check_password():
    """사용자가 입력한 비밀번호가 오늘 날짜 8자리와 일치하는지 확인합니다."""
    def password_entered():
        correct_password = get_today_password()
        if st.session_state["password_input"] == correct_password:
            st.session_state["authenticated"] = True
            del st.session_state["password_input"]  # 보안을 위해 입력값 즉시 삭제
            st.toast("🟢 로그인에 성공했습니다!", icon="✅")
        else:
            st.session_state["authenticated"] = False
            st.error("❌ 비밀번호가 올바르지 않습니다. 비밀번호를 모르실 경우 관리자에게 문의해 주세요.")

    if not st.session_state["authenticated"]:
        st.subheader("🔐 재고조회 패스워드 입력")
        st.caption("본 시스템은 사내 관계자 전용 대시보드입니다.")
        
        st.text_input(
            "영업팀 전용 비밀번호를 입력하세요",
            type="password",
            on_change=password_entered,
            key="password_input",
            placeholder="비밀번호를 입력한 후 Enter 키를 눌러주세요"
        )
        st.stop()  # 인증 전에는 아래 데이터를 로드하거나 렌더링하지 않음

# 로그인 가드 실행
check_password()

# =========================================================================
# [유틸] 4. 타입 안정성 및 포맷팅 헬퍼 함수
# =========================================================================
def safe_holding_months(val):
    """PyArrow 타입 변환 충돌(숫자 + 빈문자열 혼합 에러)을 방지하는 안전 문자열 변환"""
    if pd.isna(val) or str(val).strip() == "":
        return ""
    try:
        return str(int(float(val)))
    except Exception:
        return str(val).strip()

def format_currency(val):
    """단가 컬럼 천 단위 콤마 포맷팅"""
    if pd.isna(val) or str(val).strip() == "":
        return ""
    try:
        num = int(float(str(val).replace(",", "").replace("₩", "").replace("원", "").strip()))
        return f"{num:,}"
    except Exception:
        return str(val).strip()

# =========================================================================
# [데이터 파이프라인] 5. 구글 스프레드시트 원격 로드 (오류 예방 보완 완료)
# =========================================================================
@st.cache_data(ttl=60)
def load_cloud_sheet_data():
    """구글 스프레드시트(dkcns_inventory)에서 사내 PC가 업로드해둔 최신 데이터 로드"""
    if not GCP_INFO:
        raise ValueError("Streamlit Secrets에 [gcp_service_account] 설정이 없습니다. Advanced settings의 Secrets를 확인해 주세요.")
    
    service_dict = dict(GCP_INFO)
    
    # [★원천 차단 패치] 기존 도메인 오류 방지를 위해 공식 구글 토큰 URI로 강제 주입
    service_dict["token_uri"] = "https://oauth2.googleapis.com/token"
    
    if "private_key" in service_dict:
        service_dict["private_key"] = service_dict["private_key"].replace("\\\\n", "\\n")
        
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    
    credentials = Credentials.from_service_account_info(service_dict, scopes=scopes)
    client = gspread.authorize(credentials)
    
    doc = client.open("dkcns_inventory")
    sheet = doc.worksheet("실시간재고현황")
    data = sheet.get_all_records()
    df = pd.DataFrame(data)
    
    # 데이터 업데이트 일시 확인 (재고검증요약 탭 B1 셀에서 취득)
    last_updated_str = ""
    try:
        summary_sheet = doc.worksheet("재고검증요약")
        val = summary_sheet.acell("B1").value
        if val:
            last_updated_str = str(val).strip()
    except Exception:
        pass
        
    if not last_updated_str:
        if not df.empty and "입고일" in df.columns:
            last_updated_str = "연동 완료 (실시간)"
        else:
            last_updated_str = "확인 불가"
            
    # PyArrow 에러 방지: 보유개월수 문자열 변환
    if "보유개월수" in df.columns:
        df["보유개월수"] = df["보유개월수"].apply(safe_holding_months)
        
    # 단가 천단위 콤마 포맷팅
    if "입고단가" in df.columns:
        df["입고단가"] = df["입고단가"].apply(format_currency)
        
    return df, last_updated_str

# =========================================================================
# [실행] 6. 데이터 적재 및 정제
# =========================================================================
try:
    df_inventory, update_datetime = load_cloud_sheet_data()
except Exception as e:
    st.error(f"🚨 구글 스프레드시트 데이터 로드 중 오류가 발생했습니다: {e}")
    st.info("💡 Secrets의 GCP 키 정보 또는 사내 PC upload.py 실행 여부를 확인해 주세요.")
    st.stop()

# -------------------------------------------------------------------------
# [데이터 가공] 미등록 항목 빈칸 처리 및 최하단 정렬
# -------------------------------------------------------------------------
if not df_inventory.empty:
    # 1. 미등록 데이터 판별용 정렬 가중치 생성 (정상: 0, 미등록: 1)
    is_unregistered = (
        df_inventory["ERP_품명"].astype(str).str.contains("미등록", na=False) |
        df_inventory["거래처명"].astype(str).str.contains("미등록", na=False) |
        (df_inventory.get("재고상태", pd.Series([""] * len(df_inventory))).astype(str).str.contains("미기안", na=False))
    )
    df_inventory["_sort_key"] = is_unregistered.astype(int)
    
    # 2. 미등록 데이터를 하단에 깔고 업체상품코드로 오름차순 정렬
    df_inventory = df_inventory.sort_values(by=["_sort_key", "대원_업체상품코드"], ascending=[True, True])
    df_inventory = df_inventory.drop(columns=["_sort_key"])
    
    # 3. '미등록 품명', '미등록 거래처' 텍스트를 공백("")으로 치환
    replace_targets = ["미등록 품명", "미등록 거래처", "미등록품명", "미등록거래처", "nan", "None", "<NA>"]
    for col in ["ERP_품명", "거래처명", "대분류", "중분류", "소분류"]:
        if col in df_inventory.columns:
            df_inventory[col] = df_inventory[col].astype(str).replace(replace_targets, "").str.strip()

# =========================================================================
# [UI] 7. 대시보드 화면 렌더링
# =========================================================================
st.title("📦 재고조회")
st.caption(f"🕒 **데이터 업데이트 날짜:** {update_datetime}")

# 탭 설정
tab1, tab2 = st.tabs(["📋 창고 연동 재고 리스트", "🤖 AI 챗봇에게 물어보기"])

# -------------------------------------------------------------------------
# [탭 1] 📋 창고 연동 재고 리스트
# -------------------------------------------------------------------------
with tab1:
    # 대분류, 중분류, 소분류 필터링 (최신 3단 연동 구조)
    f_col1, f_col2, f_col3 = st.columns(3)
    with f_col1:
        cat_major_opts = sorted([x for x in df_inventory["대분류"].dropna().unique() if str(x).strip() != ""]) if "대분류" in df_inventory.columns else []
        selected_major = st.multiselect("대분류 필터링", options=cat_major_opts, default=[])
    with f_col2:
        if selected_major and "대분류" in df_inventory.columns:
            sub_df = df_inventory[df_inventory["대분류"].isin(selected_major)]
        else:
            sub_df = df_inventory
        cat_mid_opts = sorted([x for x in sub_df["중분류"].dropna().unique() if str(x).strip() != ""]) if "중분류" in sub_df.columns else []
        selected_mid = st.multiselect("중분류 필터링", options=cat_mid_opts, default=[])
    with f_col3:
        if selected_mid and "중분류" in sub_df.columns:
            sub_df2 = sub_df[sub_df["중분류"].isin(selected_mid)]
        else:
            sub_df2 = sub_df
        cat_minor_opts = sorted([x for x in sub_df2["소분류"].dropna().unique() if str(x).strip() != ""]) if "소분류" in sub_df2.columns else []
        selected_minor = st.multiselect("소분류 필터링", options=cat_minor_opts, default=[])

    # 검색창
    search_query = st.text_input("🔍 품명, 규격, 업체상품코드, 거래처명 통합 검색")

    # 필터 적용
    df_filtered = df_inventory.copy()
    if selected_major and "대분류" in df_filtered.columns:
        df_filtered = df_filtered[df_filtered["대분류"].isin(selected_major)]
    if selected_mid and "중분류" in df_filtered.columns:
        df_filtered = df_filtered[df_filtered["중분류"].isin(selected_mid)]
    if selected_minor and "소분류" in df_filtered.columns:
        df_filtered = df_filtered[df_filtered["소분류"].isin(selected_minor)]

    if search_query:
        searchable_cols = [c for c in ["대원_업체상품코드", "대원_상품명", "ERP_품명", "적요(규격)", "거래처명"] if c in df_filtered.columns]
        if searchable_cols:
            df_filtered = df_filtered[
                df_filtered[searchable_cols].apply(
                    lambda row: row.astype(str).str.contains(search_query, case=False).any(),
                    axis=1
                )
            ]

    # 14개 컬럼 순서 고정 (대/중/소분류 최전방 배치)
    target_columns = [
        "대분류",
        "중분류",
        "소분류",
        "대원_업체상품코드",
        "대원_상품명",
        "대원_현재재고",
        "대원_가용재고",
        "ERP_품명",
        "적요(규격)",
        "거래처명",
        "ERP_재고수량",
        "입고단가",
        "입고일",
        "보유개월수"
    ]
    display_columns = [col for col in target_columns if col in df_filtered.columns]
    df_display = df_filtered[display_columns].reset_index(drop=True)

    st.markdown(f"총 **{len(df_display):,}** 건의 품목이 조회되었습니다.")
    st.dataframe(df_display, use_container_width=True, height=550)

# -------------------------------------------------------------------------
# [탭 2] 🤖 AI 챗봇에게 물어보기
# -------------------------------------------------------------------------
with tab2:
    st.subheader("💬 실재고 관련 궁금한 점을 편하게 질문하세요")
    st.caption("💡 32G 메모리 재고, 거래처별 재고 등 실재고 수량을 자연어로 질의할 수 있습니다.")
    
    user_question = st.chat_input("질문 예시: '32기가 메모리 재고 있어?', '하루제스트 재고 목록?'")
    if user_question:
        with st.chat_message("user"):
            st.write(user_question)
            
       with st.chat_message("assistant"):
            if not CLEAN_API_KEY or CLEAN_API_KEY.startswith("AIzaSy...") or len(CLEAN_API_KEY) < 20:
                st.warning("⚠️ 챗봇 기능 활성화를 위해 Streamlit Secrets에 올바른 Gemini API Key를 등록해 주세요.")
            else:
                try:
                    genai.configure(api_key=CLEAN_API_KEY)

                    # 최신 안정화 모델인 gemini-1.5-flash 모델로 변경 적용
                    model = genai.GenerativeModel("gemini-1.5-flash")

                    # 챗봇에 전달할 컨텍스트 컬럼 정의
                    ai_context_cols = [c for c in target_columns if c in df_inventory.columns]
                    context_csv = df_inventory[ai_context_cols].to_csv(index=False)

                    prompt = f"""
                    너는 팀의 재고 및 오더 검증 전문가야.
                    아래 창고 연동 실재고 데이터(CSV)에 기반해서만 답변하고, 없는 이야기는 지어내지 마.
                    사용자가 32G, 64G 등 메모리 스펙이나 특정 부품 규격으로 질문할 경우 '대원_상품명', '적요(규격)', '대원_업체상품코드' 컬럼을 복합 탐색하여 대원 실재고 수량을 바탕으로 명확히 답해줘.
                    질문 이외의 불필요한 시스템 안내는 생략하고 영업 담당자에게 필요한 핵심 재고 정보만 일목요연하게 표나 글머리기호로 정리해.
                    금액 단위는 세 자리 콤마와 원화(원)로 표기해.
                    [업데이트 일시]: {update_datetime}
                    [창고 연동 실재고 데이터]
                    {context_csv}
                    [영업 담당자 질문]
                    {user_question}
                    """
                    response = model.generate_content(prompt)
                    st.markdown(response.text)
                except Exception as e:
                    st.error(f"AI 호출 중 에러 발생: {e}")
