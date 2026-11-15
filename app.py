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

# CSS 스타일 적용 (모바일 1라인 강제 유지, 우측 상단 기본 'Running...' 숨김, 스크롤바 가시성 증대)
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
    
    # 패스워드가 이미 맞게 통과된 상태라면 로그인창을 건너뜜
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
            
    st.markdown("### 🔑 패스워드 입력")
    st.text_input(
        "비밀번호를 입력하세요",
        type="password",
        on_change=password_entered,
        key="password_input"
    )
    
    if "password_correct" in st.session_state:
        if not st.session_state["password_correct"]:
            st.error("비밀번호가 올바르지 않습니다. 관리자에게 문의해 주세요.")
        else:
            # 패스워드가 올바를 경우 즉시 화면을 갱신하여 메인 앱 로딩으로 진입시킴
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
            
        # [안전 장치 추가] get_all_records()의 빈 헤더 중복 에러 및 타입에러를 원천 방지하는 안전 함수
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
            
            # 공백 헤더명('') 및 Unnamed 컬럼을 완벽 제거
            df = df.loc[:, df.columns.astype(str).str.strip() != '']
            df = df.loc[:, ~df.columns.astype(str).str.contains('^Unnamed:')]
            
            # 숫자 자동 형변환 진행
            for col in df.columns:
                try:
                    df[col] = pd.to_numeric(df[col], errors='raise')
                except Exception:
                    pass
                
            return df
            
        # 1. 안전한 함수를 이용하여 실시간재고현황 로드
        sheet_realtime = spreadsheet.worksheet("실시간재고현황")
        df_inv = safe_get_dataframe(sheet_realtime)
        
        # 2. 안전한 함수를 이용하여 재고검증요약 로드
        sheet_summary = spreadsheet.worksheet("재고검증요약")
        df_sum = safe_get_dataframe(sheet_summary)
        
        # 3. 업데이트 탭 연동하여 업로드 실시간 취득 (A1:B1에서 B1 값 획득)
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
        
        return df_inv, df_sum, update_datetime_val

    # -------------------------------------------------------------------------
    # 🛠️ 데이터 로드 및 초기 정제
    # -------------------------------------------------------------------------
    df_inventory = pd.DataFrame()
    df_summary = pd.DataFrame()
    update_datetime_raw = ""
    data_loaded = False
    
    # 플레이스홀더를 사용해 로딩 안내문을 임시 생성
    status_holder = st.empty()
    status_holder.info("🔑 인증이 완료되었습니다. 데이터를 불러오는 중입니다...")
    
    try:
        df_inventory, df_summary, update_datetime_raw = load_cloud_sheet_data()
        data_loaded = True
        status_holder.empty()
    except Exception as general_err:
        status_holder.empty()
        st.error(f"❌ 데이터 로드 중 예상치 못한 치명적 에러가 발생했습니다: {general_err}")

    # 데이터가 정상 로드되었을 때만 후속 로직 진행
    if data_loaded:
        # 구글시트 업데이트 시간 포맷팅
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
        
        # 대/중/소분류 빈칸도 정상 항목 내에서는 아래로 가도록 보조 정렬키 생성
        df_inventory["_cat1"] = df_inventory["대분류"].apply(lambda x: "ㅎㅎㅎ" if x == "" else x)
        df_inventory["_cat2"] = df_inventory["중분류"].apply(lambda x: "ㅎㅎㅎ" if x == "" else x)
        df_inventory["_cat3"] = df_inventory["소분류"].apply(lambda x: "ㅎㅎㅎ" if x == "" else x)
        df_inventory = df_inventory.sort_values(
            by=["_sort_weight", "_cat1", "_cat2", "_cat3"],
            ascending=[True, True, True, True]
        ).drop(columns=["_sort_weight", "_cat1", "_cat2", "_cat3"]).reset_index(drop=True)

        # -------------------------------------------------------------------------
        # 📋 컬럼명 유연 매칭 및 "입고일", "보유개월수" 동적 이름 지정
        # -------------------------------------------------------------------------
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
            
            # 2. 대분류 / 중분류 / 소분류 필터 가로 배치
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

            # 4. '순번' 컬럼을 생성하고, 오직 지정된 15개의 컬럼만 최종 화면에 표시 (이외 품목 비표시)
            fixed_columns = [
                "대분류", "중분류", "소분류", 
                "대원_업체상품코드", "대원_상품명", "대원_현재재고", "대원_가용재고", 
                "ERP_품명", "적요(규격)", "거래처명", "ERP_재고수량", 
                "입고단가", "입고일", "보유개월수"
            ]
            
            # 없는 컬럼 대비 보완
            for col in fixed_columns:
                if col not in df_filtered.columns:
                    df_filtered[col] = ""
                    
            # 지정된 열 이외에는 표시하지 않도록 정확하게 슬라이싱 (비고, 상태 등 완전 비표시)
            df_ordered = df_filtered[fixed_columns].copy()
            
            # -------------------------------------------------------------------------
            # 💲 데이터 정밀 포맷팅 (입고단가 콤마 추가, 입고일 YYYY-MM-DD 강제)
            # -------------------------------------------------------------------------
            def format_price(val):
                if pd.isna(val) or str(val).strip() in ["", "nan", "None"]:
                    return ""
                try:
                    num = float(str(val).replace(",", "").strip())
                    return f"{int(num):,}"
                except ValueError:
                    return val

            def format_date(val):
                if pd.isna(val) or str(val).strip() in ["", "nan", "None"]:
                    return ""
                try:
                    dt_parsed = pd.to_datetime(val)
                    return dt_parsed.strftime('%Y-%m-%d')
                except Exception:
                    return str(val).split(" ")[0].strip()

            df_ordered["입고단가"] = df_ordered["입고단가"].apply(format_price)
            df_ordered["입고일"] = df_ordered["입고일"].apply(format_date)
            
            # '순번' 컬럼을 가장 맨 앞에 1부터 넘버링하여 삽입
            df_ordered = df_ordered.reset_index(drop=True)
            df_ordered.insert(0, "순번", range(1, len(df_ordered) + 1))
            
            # 최종 데이터 프레임 표출
            st.dataframe(df_ordered, use_container_width=True, hide_index=True)

        with tab2:
            # 탭 메뉴를 클릭하자마자 즉시 "개발중 입니다." 팝업(Toast)과 알림창(Info) 표출 (요청 반영)
            st.toast("💬 개발중 입니다.", icon="⚠️")
            st.info("💬 현재 이 메뉴는 개발 중입니다. 빠른 시일 내에 찾아뵙겠습니다!")
