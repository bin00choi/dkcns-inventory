import datetime
import pytz
import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import google.generativeai as genai

# ==========================================
# 1. 페이지 기본 설정
# ==========================================
st.set_page_config(
    page_title="재고조회",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# ==========================================
# 2. 보안 로그인 게이트 (오늘 날짜 8자리, 힌트 제거)
# ==========================================
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
        # 요청하신 간결하고 명확한 문구로 전면 교체
        st.subheader("🔐 재고조회 패스워드 입력")
        st.caption("본 시스템은 사내 관계자 전용 대시보드입니다.")

        st.text_input(
            "영업팀 전용 비밀번호를 입력하세요",
            type="password",
            on_change=password_entered,
            key="password_input",
            placeholder="비밀번호를 입력한 후 Enter 키를 눌러주세요"
        )
        st.stop()  # 인증 전에는 하단 코드 실행 및 API 호출 완전 차단

# 로그인 체크 실행
check_password()

# ==========================================
# 3. 구글 스프레드시트 데이터 로드 (캐싱 적용)
# ==========================================
@st.cache_data(ttl=60)
def load_inventory_data():
    """구글 스프레드시트에서 실시간 재고 데이터와 업데이트 일시를 불러옵니다."""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    
    # Streamlit Secrets에서 서비스 계정 정보 로드 및 token_uri 보정
    creds_dict = dict(st.secrets["gcp_service_account"])
    if "token_uri" not in creds_dict or not creds_dict["token_uri"]:
        creds_dict["token_uri"] = "https://oauth2.googleapis.com/token"
        
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    
    # 스프레드시트 열기
    sh = client.open("dkcns_inventory")
    
    # 1) 실시간재고현황 데이터 로드
    ws_data = sh.worksheet("실시간재고현황")
    data = ws_data.get_all_records()
    df = pd.DataFrame(data)
    
    # 2) 재고검증요약 탭에서 업데이트 일시(A1:B1) 로드
    update_time_str = "업데이트 정보 없음"
    try:
        ws_summary = sh.worksheet("재고검증요약")
        summary_vals = ws_summary.get("A1:B1")
        if summary_vals and len(summary_vals[0]) >= 2:
            update_time_str = f"{summary_vals[0][0]}: {summary_vals[0]}"
        elif summary_vals and len(summary_vals[0]) == 1:
            update_time_str = str(summary_vals[0][0])
    except Exception:
        pass
        
    return df, update_time_str

# 데이터 불러오기 실행
try:
    df_raw, last_updated = load_inventory_data()
except Exception as e:
    st.error(f"🚨 데이터를 불러오는 중 오류가 발생했습니다: {e}")
    st.stop()

# ==========================================
# 4. 데이터 전처리 (정렬, 빈칸 치환, 타입 보정)
# ==========================================
df = df_raw.copy()

if not df.empty:
    # (1) 필수 컬럼이 없을 경우를 대비해 기본 빈 컬럼 보장
    required_cols = [
        "대분류", "중분류", "소분류", "대원_업체상품코드", "대원_상품명",
        "대원_현재재고", "대원_가용재고", "ERP_품명", "적요(규격)",
        "거래처명", "ERP_재고수량", "입고단가", "입고일", "보유개월수"
    ]
    for col in required_cols:
        if col not in df.columns:
            df[col] = ""

    # (2) 미등록 품목 판별 및 최하단 정렬을 위한 가중치 부여 (정상: 0, 미등록/미기안: 1)
    is_unregistered = (
        df["ERP_품명"].astype(str).str.contains("미등록", na=False) |
        df["거래처명"].astype(str).str.contains("미등록", na=False) |
        (df.get("재고상태", pd.Series([""] * len(df))).astype(str).str.contains("미기안", na=False))
    )
    df["_sort_key"] = is_unregistered.astype(int)
    df = df.sort_values(by=["_sort_key", "대원_업체상품코드"], ascending=[True, True]).drop(columns=["_sort_key"])

    # (3) '미등록 품명', '미등록 거래처' 및 결측치 텍스트를 빈칸("")으로 깔끔하게 치환
    replace_targets = ["미등록 품명", "미등록 거래처", "미등록품명", "미등록거래처", "nan", "None", "<NA>"]
    for col in ["ERP_품명", "거래처명", "대분류", "중분류", "소분류", "적요(규격)"]:
        df[col] = df[col].astype(str).replace(replace_targets, "").str.strip()

    # (4) 숫자형 컬럼 안전 변환
    num_cols = ["대원_현재재고", "대원_가용재고", "ERP_재고수량", "입고단가"]
    for col in num_cols:
        df[col] = pd.to_numeric(df[col].astype(str).str.replace(",", ""), errors="coerce").fillna(0).astype(int)

    # (5) PyArrow 오류 방지를 위해 보유개월수 및 입고일은 문자열로 정제
    df["보유개월수"] = df["보유개월수"].astype(str).replace(["nan", "None", "<NA>"], "")
    df["입고일"] = df["입고일"].astype(str).replace(["nan", "None", "<NA>"], "")

# ==========================================
# 5. 메인 화면 구성 및 탭 레이아웃
# ==========================================
st.title("📦 재고조회")
st.caption(f"⏱️ 데이터 기준 일시: **{last_updated}** (구글 시트 실시간 연동)")

tab1, tab2 = st.tabs(["📋 창고 연동 재고 리스트", "🤖 AI 재고 검색 챗봇"])

# ------------------------------------------
# [탭 1] 📋 창고 연동 재고 리스트
# ------------------------------------------
with tab1:
    if df.empty:
        st.info("표시할 재고 데이터가 없습니다.")
    else:
        # 1) 3단 계층형 카테고리 필터 (대분류 ➔ 중분류 ➔ 소분류) 및 검색창
        col1, col2, col3, col4 = st.columns()
        
        with col1:
            majors = sorted([x for x in df["대분류"].unique() if x])
            sel_major = st.multiselect("대분류", majors, placeholder="전체")
            df_filtered = df[df["대분류"].isin(sel_major)] if sel_major else df
            
        with col2:
            middles = sorted([x for x in df_filtered["중분류"].unique() if x])
            sel_middle = st.multiselect("중분류", middles, placeholder="전체")
            if sel_middle:
                df_filtered = df_filtered[df_filtered["중분류"].isin(sel_middle)]
                
        with col3:
            minors = sorted([x for x in df_filtered["소분류"].unique() if x])
            sel_minor = st.multiselect("소분류", minors, placeholder="전체")
            if sel_minor:
                df_filtered = df_filtered[df_filtered["소분류"].isin(sel_minor)]
                
        with col4:
            search_kw = st.text_input("🔍 통합 검색 (상품명/코드/규격/거래처)", placeholder="검색어를 입력하세요...")
            if search_kw:
                kw = search_kw.strip()
                mask = (
                    df_filtered["대원_업체상품코드"].astype(str).str.contains(kw, case=False, na=False) |
                    df_filtered["대원_상품명"].astype(str).str.contains(kw, case=False, na=False) |
                    df_filtered["ERP_품명"].astype(str).str.contains(kw, case=False, na=False) |
                    df_filtered["적요(규격)"].astype(str).str.contains(kw, case=False, na=False) |
                    df_filtered["거래처명"].astype(str).str.contains(kw, case=False, na=False)
                )
                df_filtered = df_filtered[mask]

        # 2) 요약 지표 표시
        st.markdown(f"**총 조회 품목 수:** `{len(df_filtered):,} 개` | **대원 가용재고 합계:** `{df_filtered['대원_가용재고'].sum():,} 개`")

        # 3) 요청된 14개 컬럼 순서 고정 (대/중/소분류 최전방 배치)
        ordered_columns = [
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
        display_df = df_filtered[ordered_columns]

        # 4) 데이터프레임 출력
        st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True,
            height=600,
            column_config={
                "대원_현재재고": st.column_config.NumberColumn("대원_현재재고", format="%d"),
                "대원_가용재고": st.column_config.NumberColumn("대원_가용재고", format="%d"),
                "ERP_재고수량": st.column_config.NumberColumn("ERP_재고수량", format="%d"),
                "입고단가": st.column_config.NumberColumn("입고단가", format="%d"),
            }
        )

# ------------------------------------------
# [탭 2] 🤖 AI 재고 검색 챗봇 (Gemini 2.5 Flash)
# ------------------------------------------
with tab2:
    st.subheader("🤖 AI 재고 비서에게 물어보세요")
    st.caption("현재 조회된 실시간 재고 데이터를 바탕으로 재고 수량, 단가, 보유개월수 등을 즉시 답변해 드립니다.")

    # Gemini API 키 확인
    gemini_key = st.secrets.get("GEMINI_API_KEY", None)
    if not gemini_key:
        st.warning("⚠️ Streamlit Secrets에 `GEMINI_API_KEY`가 설정되어 있지 않아 챗봇을 사용할 수 없습니다.")
    else:
        genai.configure(api_key=gemini_key)
        
        if "messages" not in st.session_state:
            st.session_state["messages"] = [
                {"role": "assistant", "content": "안녕하세요! 찾으시는 모델명, 규격, 거래처 또는 재고 조건을 말씀해 주세요."}
            ]

        for msg in st.session_state["messages"]:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

        if user_prompt := st.chat_input("질문을 입력하세요 (예: 가용재고 10개 이상인 모니터 알려줘)"):
            st.session_state["messages"].append({"role": "user", "content": user_prompt})
            with st.chat_message("user"):
                st.markdown(user_prompt)

            with st.chat_message("assistant"):
                with st.spinner("실시간 재고 데이터를 분석 중입니다..."):
                    try:
                        # AI 컨텍스트 전달용 데이터 요약
                        context_csv = df[ordered_columns].to_csv(index=False)
                        system_prompt = f"""
                        당신은 DKCNS의 실시간 재고 관리 AI 비서입니다.
                        아래 제공된 실시간 재고 데이터(CSV 형식)만을 근거로 사용자의 질문에 정확하고 친절하게 답변하세요.
                        표(Markdown Table)를 적극 활용하여 가독성을 높이세요.
                        
                        [실시간 재고 데이터]
                        {context_csv}
                        """
                        model = genai.GenerativeModel("gemini-2.5-flash")
                        response = model.generate_content([system_prompt, user_prompt])
                        answer = response.text
                    except Exception as e:
                        answer = f"❌ AI 응답 생성 중 오류가 발생했습니다: {e}"
                        
                    st.markdown(answer)
                    st.session_state["messages"].append({"role": "assistant", "content": answer})
