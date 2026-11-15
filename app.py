import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import pytz
import requests
import json

# Page 기본 설정
st.set_page_config(page_title="재고 조회", layout="wide")

# CSS 스타일 적용 (모바일 최적화 및 레이아웃 가독성 강화)
st.markdown("""
<style>
/* 1. 우측 상단 Running... 기본 위젯만 깔끔하게 숨김 */
[data-testid="stStatusWidget"] {
    visibility: hidden;
    height: 0px !important;
    width: 0px !important;
    overflow: hidden;
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

/* 3. 모바일(640px 이하) 전용 반응형 레이아웃 및 폰트 축소 */
@media (max-width: 640px) {
    /* 모바일 전체 화면 여백 최적화 (상단 여백은 1.8rem으로 늘려 로그인창 잘림 방지) */
    .block-container {
        padding-top: 1.8rem !important;
        padding-bottom: 1.2rem !important;
        padding-left: 0.6rem !important;
        padding-right: 0.6rem !important;
    }
    
    /* 모바일 타이틀 크기 가독성 확보 */
    .mobile-title h1 {
        font-size: 2.0rem !important;
    }
    .mobile-title span {
        font-size: 14px !important;
        margin-left: 8px !important;
    }
    
    /* 대/중/소분류 다중 필터 한 줄 배치 고도화 */
    div[data-testid="stHorizontalBlock"] {
        flex-direction: row !important;
        flex-wrap: nowrap !important;
        gap: 4px !important;
    }
    div[data-testid="stHorizontalBlock"] > div {
        min-width: 0 !important;
        flex: 1 1 0% !important;
    }
    /* 모바일 셀렉트박스 텍스트 가독성 확보 */
    div[data-baseweb="select"] {
        font-size: 11px !important;
        padding: 1px !important;
    }
    
    /* 패스워드 입력창 모바일 전용 상하 정렬 여백 부여 */
    .stTextInput {
        margin-top: 15px !important;
        margin-bottom: 15px !important;
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
    
    if st.session_state.get("password_correct", False):
        return True
        
    def password_entered():
        user_input = st.session_state.get("password_input", "")
        if user_input == correct_password:
            st.session_state["password_correct"] = True
            if "password_input" in st.session_state:
                del st.session_state["password_input"]
        else:
            st.session_state["password_correct"] = False
            
    # 로그인 타이틀과 비밀번호 폼 간의 공간 확보를 위한 컨테이너 구성
    st.markdown("<div style='margin-top: 20px;'>### 🔑 패스워드 입력</div>", unsafe_allow_html=True)
    st.text_input(
        "비밀번호를 입력하세요 (오늘 날짜 YYYYMMDD)",
        type="password",
        on_change=password_entered,
        key="password_input"
    )
    
    if "password_correct" in st.session_state:
        if not st.session_state["password_correct"]:
            st.error("비밀번호가 올바르지 않습니다. 관리자에게 문의해 주세요.")
        else:
            st.rerun()
            
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
            
        # 안전한 데이터프레임 변환 함수
        def safe_get_dataframe(worksheet):
            all_values = worksheet.get_all_values()
            if not all_values:
                return pd.DataFrame()
            
            headers = [str(h).strip() for h in all_values[0]]
            data_rows = all_values[1:]
            
            df = pd.DataFrame(data_rows)
            
            if len(headers) < df.shape[1]:
                headers += [""] * (df.shape[1] - len(headers))
            elif len(headers) > df.shape[1]:
                headers = headers[:df.shape[1]]
                
            df.columns = headers
            
            # 공백 헤더명('') 및 Unnamed 컬럼 제거
            df = df.loc[:, df.columns.astype(str).str.strip() != '']
            df = df.loc[:, ~df.columns.astype(str).str.contains('^Unnamed:')]
            
            # 숫자 자동 형변환
            for col in df.columns:
                try:
                    df[col] = pd.to_numeric(df[col], errors='raise')
                except Exception:
                    pass
                
            return df
            
        # 1. 실시간재고현황 로드
        sheet_realtime = spreadsheet.worksheet("실시간재고현황")
        df_inv = safe_get_dataframe(sheet_realtime)
        
        # 2. 재고검증요약 로드
        sheet_summary = spreadsheet.worksheet("재고검증요약")
        df_sum = safe_get_dataframe(sheet_summary)
        
        # 3. 예약 시트 데이터 로드
        df_reserve = pd.DataFrame()
        try:
            sheet_reserve = spreadsheet.worksheet("예약")
            df_reserve = safe_get_dataframe(sheet_reserve)
        except Exception:
            pass
        
        # 4. 업데이트 탭 연동하여 업로드 일시 취득
        update_datetime_val = "알 수 없음"
        try:
            sheet_update = spreadsheet.worksheet("업데이트")
            update_data = sheet_update.get_all_values()
            if len(update_data) > 0 and len(update_data[0]) > 1:
                update_datetime_val = str(update_data[0][1]).strip()
        except Exception:
            try:
                update_datetime_val = spreadsheet.updated
            except Exception:
                pass
        
        return df_inv, df_sum, df_reserve, update_datetime_val

    # -------------------------------------------------------------------------
    # 🛠️ 데이터 로드 및 초기 정제
    # -------------------------------------------------------------------------
    df_inventory = pd.DataFrame()
    df_summary = pd.DataFrame()
    df_reserve_raw = pd.DataFrame()
    update_datetime_raw = ""
    data_loaded = False
    
    status_holder = st.empty()
    status_holder.info("🔑 인증이 완료되었습니다. 데이터를 불러오는 중입니다...")
    
    try:
        df_inventory, df_summary, df_reserve_raw, update_datetime_raw = load_cloud_sheet_data()
        data_loaded = True
        status_holder.empty()
    except Exception as general_err:
        status_holder.empty()
        st.error(f"❌ 데이터 로드 중 예상치 못한 치명적 에러가 발생했습니다: {general_err}")

    if data_loaded:
        # 구글시트 업데이트 시간 KST 포맷팅
        try:
            parsed_time = pd.to_datetime(update_datetime_raw)
            if parsed_time.tzinfo is None:
                parsed_time = pytz.timezone('Asia/Seoul').localize(parsed_time)
            else:
                parsed_time = parsed_time.tz_convert('Asia/Seoul')
            update_datetime = parsed_time.strftime('%Y-%m-%d %H:%M:%S KST')
        except Exception:
            update_datetime = str(update_datetime_raw)
            if not update_datetime.endswith("KST") and update_datetime != "알 수 없음":
                update_datetime = f"{update_datetime} KST"
        
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
        
        # 대/중/소분류 보조 정렬키 생성
        df_inventory["_cat1"] = df_inventory["대분류"].apply(lambda x: "ㅎㅎㅎ" if x == "" else x)
        df_inventory["_cat2"] = df_inventory["중분류"].apply(lambda x: "ㅎㅎㅎ" if x == "" else x)
        df_inventory["_cat3"] = df_inventory["소분류"].apply(lambda x: "ㅎㅎㅎ" if x == "" else x)
        df_inventory = df_inventory.sort_values(
            by=["_sort_weight", "_cat1", "_cat2", "_cat3"],
            ascending=[True, True, True, True]
        ).drop(columns=["_sort_weight", "_cat1", "_cat2", "_cat3"]).reset_index(drop=True)

        # 컬럼명 유연 매칭
        target_mappings = {
            "대분류": ["대분류"],
            "중분류": ["중분류"],
            "소분류": ["소분류"],
            "대원_업체상품코드": ["대원_업체상품코드"],
            "대원_상품명": ["대원_상품명"],
            "대원_현재재고": ["대원_현재재고", "대원_실재고수량"],
            "대원_가용재고": ["대원_가용재고"],
            "ERP_품명": ["ERP_품명"],
            "적요(규격)": ["적요(규격)"],
            "거래처명": ["거래처명", "ERP_거래처명"],
            "ERP_재고수량": ["ERP_재고수량", "창고_실재고수량"],
            "입고단가": ["입고단가", "공급가액"],
            "입고일": ["입고일(Invoice)", "입고일"],
            "보유개월수": ["보유\n\n개월수", "보유\n개월수", "보유개월수", "보유 개월수"]
        }
        rename_dict = {}
        for target_name, alternatives in target_mappings.items():
            for alt in alternatives:
                if alt in df_inventory.columns:
                    rename_dict[alt] = target_name
                    break
        
        df_inventory = df_inventory.rename(columns=rename_dict)

        # 예약재고 반영 및 가용재고 차감
        df_inventory["예약재고"] = 0
        
        if df_reserve_raw is not None and not df_reserve_raw.empty:
            col_reserve_item = None
            col_reserve_qty = None
            for col in df_reserve_raw.columns:
                if "품목" in col or "품명" in col:
                    col_reserve_item = col
                if "수량" in col or "재고" in col:
                    col_reserve_qty = col
            
            if col_reserve_item and col_reserve_qty:
                df_reserve_raw[col_reserve_qty] = pd.to_numeric(df_reserve_raw[col_reserve_qty], errors='coerce').fillna(0).astype(int)
                reserve_summary = df_reserve_raw.groupby(col_reserve_item)[col_reserve_qty].sum().to_dict()
                
                for idx, row in df_inventory.iterrows():
                    erp_item_name = str(row.get("ERP_품명", "")).strip()
                    if erp_item_name in reserve_summary:
                        res_qty = int(reserve_summary[erp_item_name])
                        df_inventory.at[idx, "예약재고"] = res_qty
                        
                        curr_avail = int(pd.to_numeric(row.get("대원_가용재고", 0), errors="coerce"))
                        df_inventory.at[idx, "대원_가용재고"] = max(0, curr_avail - res_qty)

        # -------------------------------------------------------------------------
        # 🖥️ 메인 UI 화면 구성
        # -------------------------------------------------------------------------
        st.markdown("""
            <div class="mobile-title" style="display: flex; align-items: baseline; margin-top: 10px; margin-bottom: 2px;">
                <h1 style="margin: 0; display: inline-block; font-size: 2.3rem; font-weight: bold; color: #111111;">🔍 재고 조회</h1>
                <span style="font-size: 15px; color: #888888; margin-left: 12px; font-weight: normal;">by CHOI</span>
            </div>
        """, unsafe_allow_html=True)
        
        st.markdown(f"<p style='margin: 0 0 10px 0; font-size: 14px; color: #666;'>마지막 업데이트: {update_datetime}</p>", unsafe_allow_html=True)
        st.markdown("<hr style='margin: 4px 0 14px 0; border: none; border-top: 1px solid #e0e0e0;'>", unsafe_allow_html=True)
        
        tab1, tab2 = st.tabs(["📋 실시간 재고 리스트", "💬 AI 챗봇에게 물어보기"])
        
        with tab1:
            # 1. 통합 검색창
            search_query = st.text_input("🔍 품목명 / 코드 등 검색", "", placeholder="원하시는 검색어를 입력하시면 목록이 필터링됩니다.")
            
            # 2. 대분류 / 중분류 / 소분류 필터
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
                
