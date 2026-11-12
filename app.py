import os
import datetime
import pytz
import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import google.generativeai as genai

# 1. 페이지 기본 설정 및 스타일 정의
st.set_page_config(page_title="재고조회", layout="wide", initial_sidebar_state="collapsed")

# 2. 세션 상태(Session State) 초기화
if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

# 3. 한국 시간 기준 오늘 날짜 8자리 생성 함수
def get_today_password():
    """한국 시간(KST) 기준으로 오늘 날짜 8자리 문자열(YYYYMMDD)을 반환합니다."""
    kst = pytz.timezone("Asia/Seoul")
    today_kst = datetime.datetime.now(kst)
    return today_kst.strftime("%Y%m%d")

# 4. 로그인 인증 함수 정의
def check_password():
    """사용자가 입력한 비밀번호가 오늘 날짜 8자리와 일치하는지 확인합니다."""
    def password_entered():
        correct_password = get_today_password()

        if st.session_state["password_input"] == correct_password:
            st.session_state["authenticated"] = True
            del st.session_state["password_input"]  # 보안을 위해 입력값 삭제
            st.toast("🟢 로그인에 성공했습니다!", icon="✅")
        else:
            st.session_state["authenticated"] = False
            # 비밀번호가 틀렸을 때 구체적인 힌트를 주지 않고 관리자 문의 메시지 표시
            st.error("❌ 비밀번호가 올바르지 않습니다. 비밀번호를 모르실 경우 관리자에게 문의해 주세요.")

    # 미인증 상태일 때 로그인 화면 표시
    if not st.session_state["authenticated"]:
        st.subheader("🔐 DKCNS 재고조회 시스템 로그인")
        st.caption("본 시스템은 사내 관계자 및 외부 영업팀 전용 대시보드입니다.")

        # 보안 극대화: 힌트(help)를 아예 제거하고, 입력창 안의 기본 문구만 표시
        st.text_input(
            "영업팀 전용 비밀번호를 입력하세요",
            type="password",
            on_change=password_entered,
            key="password_input",
            placeholder="비밀번호를 입력한 후 Enter 키를 눌러주세요"
        )
        st.stop()  # 인증 성공 전에는 아래 코드로 진행하지 않음

# 5. 로그인 체크 실행
check_password()

# =========================================================================
# [설정] 1. 웹 대시보드 기본 환경 설정 (스마트폰 모바일 뷰 최적화)
# =========================================================================
st.set_page_config(
    page_title="재고조회",
    page_icon="📦",
    layout="wide"
)

# =========================================================================
# [보안] 2. 인증 키 안전 로드 (Streamlit Secrets)
# =========================================================================
# 1) Gemini API Key 추출
try:
    RAW_GEMINI_KEY = st.secrets["GEMINI_API_KEY"]
except Exception:
    RAW_GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")

CLEAN_API_KEY = str(RAW_GEMINI_KEY).strip().replace("'", "").replace('"', "")

# 2) 구글 서비스 계정 인증 정보 추출 (Streamlit Secrets [gcp_service_account])
try:
    GCP_INFO = dict(st.secrets["gcp_service_account"])
except Exception:
    GCP_INFO = None


# =========================================================================
# [유틸] 3. PyArrow 타입 안정성 헬퍼 함수
# =========================================================================
def safe_holding_months(val):
    """PyArrow 타입 변환 충돌(숫자 + 빈문자열 혼합 에러)을 원천 차단하는 문자열 변환"""
    if pd.isna(val) or str(val).strip() == "":
        return ""
    try:
        return str(int(float(val)))
    except Exception:
        return str(val).strip()


# =========================================================================
# [데이터 파이프라인] 4. 구글 스프레드시트 원격 로드
# ※ 로컬 파일 대사/업로드는 사내 PC의 upload.py가 전담하므로, app.py는 시트만 조회합니다.
# =========================================================================
@st.cache_data(ttl=60)
def load_cloud_sheet_data():
    """구글 스프레드시트(dkcns_inventory)에서 사내 PC가 업로드해둔 최신 대사 데이터를 원격 조회"""
    if not GCP_INFO:
        raise ValueError("Streamlit Secrets에 [gcp_service_account] 설정이 없습니다. Advanced settings의 Secrets를 확인해 주세요.")

    service_dict = dict(GCP_INFO)

    # 1) 도메인 오타 자동 보정 (NameResolutionError 원천 방지)
    # oauth2.google.com -> oauth2.googleapis.com
    if "token_uri" in service_dict:
        service_dict["token_uri"] = service_dict["token_uri"].replace("oauth2.google.com", "oauth2.googleapis.com")
    else:
        service_dict["token_uri"] = "https://oauth2.googleapis.com/token"

    if "auth_uri" in service_dict:
        service_dict["auth_uri"] = service_dict["auth_uri"].replace("accounts.google.com/o/oauth2/auth", "accounts.google.com/o/oauth2/auth")

    # 2) private_key 개행문자(\n) 보정
    if "private_key" in service_dict:
        service_dict["private_key"] = service_dict["private_key"].replace("\\n", "\n")

    # 3) 구글 공식 최신 google-auth 인증 방식 적용 (gspread 권장 방식)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    credentials = Credentials.from_service_account_info(service_dict, scopes=scopes)
    client = gspread.authorize(credentials)

    # 사내 PC의 upload.py가 업로드한 dkcns_inventory 스프레드시트 열기
    doc = client.open("dkcns_inventory")
    sheet = doc.worksheet("실시간재고현황")
    data = sheet.get_all_records()
    df = pd.DataFrame(data)

    # PyArrow 에러 방지를 위해 보유개월수 컬럼 문자열 타입 보정
    if "보유개월수" in df.columns:
        df["보유개월수"] = df["보유개월수"].apply(safe_holding_months)

    return df


# =========================================================================
# [실행] 5. 데이터 적재
# =========================================================================
try:
    df_inventory = load_cloud_sheet_data()
except Exception as e:
    st.error(f"🚨 구글 스프레드시트 데이터 로드 중 오류가 발생했습니다: {e}")
    st.info("💡 Secrets의 GCP 키 정보 또는 사내 PC upload.py 실행 여부를 확인해 주세요.")
    st.stop()


# =========================================================================
# [UI] 6. 대시보드 화면 렌더링
# =========================================================================
st.title("📦 재고조회")
st.caption("📂 구글 스프레드시트(dkcns_inventory)와 실시간 연동된 클라우드 대시보드입니다.")

# 탭 구성: 1) 실시간 재고 상세 조회, 2) AI 에이전트 대화
tab1, tab2 = st.tabs(["🔍 실시간 재고 상세 조회", "🤖 AI 챗봇에게 물어보기"])

with tab1:
    col1, col2 = st.columns(2)
    with col1:
        status_options = df_inventory["상태"].unique() if "상태" in df_inventory.columns else []
        selected_status = st.multiselect("재고 진행상태 필터링", options=status_options, default=[])
    with col2:
        search_query = st.text_input("거래처명, 상품코드 또는 규격 검색")

    df_filtered = df_inventory.copy()
    if selected_status:
        df_filtered = df_filtered[df_filtered["상태"].isin(selected_status)]
    if search_query:
        target_cols = [c for c in ["거래처명", "대원_업체상품코드", "대원_상품명", "적요(규격)", "대원_상품코드", "대분류", "중분류"] if c in df_filtered.columns]
        if target_cols:
            df_filtered = df_filtered[
                df_filtered[target_cols].apply(lambda row: row.astype(str).str.contains(search_query, case=False).any(), axis=1)
            ]

    st.dataframe(df_filtered, use_container_width=True, height=520)


with tab2:
    st.subheader("💬 실재고 관련 궁금한 점을 편하게 질문하세요")
    st.caption("💡 영업 현장에서 스마트폰으로 32G 메모리 재고, 거래처별 납품 잔량, 선결제 미출고 현황 등을 자연어로 질의할 수 있습니다.")

    user_question = st.chat_input("질문 예시: '32기가 메모리 탑재 장비 재고 있어?', '선결제 미출고 장비 현황은?'")

    if user_question:
        with st.chat_message("user"):
            st.write(user_question)

        with st.chat_message("assistant"):
            if not CLEAN_API_KEY or CLEAN_API_KEY.startswith("AIzaSy...") or len(CLEAN_API_KEY) < 20:
                st.warning("⚠️ 챗봇 기능 활성화를 위해 Streamlit Secrets에 올바른 Gemini API Key를 등록해 주세요.")
            else:
                try:
                    genai.configure(api_key=CLEAN_API_KEY)
                    model = genai.GenerativeModel("gemini-2.5-flash")

                    # 구글 시트에서 가져온 대사 데이터를 CSV 형식 텍스트로 변환해 AI 컨텍스트에 주입
                    context_csv = df_inventory.to_csv(index=False)

                    prompt = f"""
                    너는 Daekyo CNS 영업팀의 재고 및 오더 검증 전문가야.
                    아래 실시간 재고 대사 데이터(CSV)에 기반해서만 답변하고, 없는 이야기는 지어내지 마.
                    사용자가 32G, 64G 등 메모리 스펙이나 특정 부품 규격으로 질문할 경우 '대원_상품명', '적요(규격)', '대원_업체상품코드' 컬럼을 복합 탐색하여 대원 실재고 수량을 바탕으로 명확히 답해줘.
                    질문 이외의 개인정보나 불필요한 시스템 안내는 생략하고 핵심 정보만 명확히 답변해.
                    금액 단위는 세련되게 세 자리 콤마와 원화(원)로 표기해.

                    [실시간 재고 대사 데이터]
                    {context_csv}

                    [영업 담당자 질문]
                    {user_question}
                    """
                    response = model.generate_content(prompt)
                    st.markdown(response.text)
                except Exception as e:
                    st.error(f"AI 호출 중 에러 발생: {e}")
