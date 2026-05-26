#!/bin/bash

# 워크로드 실행 스크립트
# 사용: ./run_workloads.sh [워크로드_폴더_경로]
# 예: ./run_workloads.sh /home/rebellions/yeun/testvector/cr13/v3.2.0

# ============================================
# 설정
# ============================================

# 워크로드 폴더 경로 (기본값 또는 인자로 전달)
WORKLOAD_DIR="${1:-/home/rebellions/yeun/testvector/cr13/v3.2.0}"

# 절대 경로로 변환
WORKLOAD_DIR="$(cd "$WORKLOAD_DIR" 2>/dev/null && pwd)" || {
    echo "❌ 에러: 워크로드 폴더를 찾을 수 없습니다: $1"
    exit 1
}

echo "==============================================="
echo "🚀 워크로드 실행 스크립트"
echo "==============================================="
echo "📂 워크로드 폴더: $WORKLOAD_DIR"
echo ""

# ============================================
# .bin 파일 리스트 생성
# ============================================

mapfile -t BIN_FILES < <(find "$WORKLOAD_DIR" -maxdepth 1 -name "*.bin" -type f | sort)

if [[ ${#BIN_FILES[@]} -eq 0 ]]; then
    echo "❌ 에러: .bin 파일을 찾을 수 없습니다"
    exit 1
fi

echo "📋 사용 가능한 워크로드:"
echo ""
for i in "${!BIN_FILES[@]}"; do
    filename=$(basename "${BIN_FILES[$i]}")
    echo "  [$((i + 1))] $filename"
done
echo ""

# ============================================
# 워크로드 선택
# ============================================

while true; do
    read -p "▶️  실행할 워크로드 번호를 선택하세요 (1-${#BIN_FILES[@]}): " choice

    # 유효성 검사
    if [[ ! "$choice" =~ ^[0-9]+$ ]] || ((choice < 1 || choice > ${#BIN_FILES[@]})); then
        echo "❌ 잘못된 선택입니다. 다시 시도하세요."
        continue
    fi

    selected_file="${BIN_FILES[$((choice - 1))]}"
    selected_name=$(basename "$selected_file")
    echo ""
    echo "✅ 선택됨: $selected_name"
    break
done

# ============================================
# 실행 시간 입력
# ============================================

while true; do
    read -p "▶️  실행 시간을 입력하세요 (초): " exec_time

    # 유효성 검사 (양의 정수만)
    if [[ ! "$exec_time" =~ ^[0-9]+$ ]] || ((exec_time <= 0)); then
        echo "❌ 올바른 숫자를 입력하세요 (0보다 큰 정수)"
        continue
    fi

    echo ""
    echo "✅ 실행 시간: ${exec_time}초"
    break
done

# ============================================
# 명령 실행
# ============================================

echo ""
echo "==============================================="
echo "🔄 실행 중..."
echo "==============================================="
echo ""

CMD="rblntrace retrace --get_perf=2 --infer_idle_time_us=0 \"$selected_file\" -e${exec_time}"

echo "📝 실행 명령:"
echo "$CMD"
echo ""

# ============================================
# 준비 상태 및 프로그레스 바 함수
# ============================================
show_preparing() {
    local elapsed=$1
    printf "\r⏳ 준비중..... (%d초)" "$elapsed"
}

show_progress() {
    local current=$1
    local total=$2
    local width=40
    local percentage=$((current * 100 / total))
    local filled=$((current * width / total))
    
    printf "\r["
    printf "%${filled}s" | tr ' ' '='
    printf "%$((width - filled))s" | tr ' ' '-'
    printf "] %3d%% (%d/%d초)" "$percentage" "$current" "$total"
}

# 임시 파일에 명령 출력 저장
TEMP_OUTPUT=$(mktemp)

# 명령 실행 (표준 출력/에러를 임시 파일로)
eval "$CMD" > "$TEMP_OUTPUT" 2>&1 &
CMD_PID=$!

# 준비 상태 모니터링 (제1 단계: "perf(us)" 대기)
prep_elapsed=0
while kill -0 $CMD_PID 2>/dev/null; do
    if grep -q "perf(us)" "$TEMP_OUTPUT" 2>/dev/null; then
        # "perf(us)" 감지 → 실제 워크로드 실행 시작 확인
        echo ""  # 줄바꿈
        break
    fi
    show_preparing "$prep_elapsed"
    sleep 1
    ((prep_elapsed++))
done

# 프로그레스 바 표시 (제2 단계: 실행 중)
if kill -0 $CMD_PID 2>/dev/null; then
    for ((elapsed=0; elapsed<=exec_time; elapsed++)); do
        show_progress "$elapsed" "$exec_time"
        sleep 1
        if ! kill -0 $CMD_PID 2>/dev/null; then
            break
        fi
    done
fi

# 최종 대기
wait $CMD_PID
RESULT=$?

# 출력 표시 (Perf 결과는 제외)
echo ""
cat "$TEMP_OUTPUT" | grep -v "^Perf"
rm -f "$TEMP_OUTPUT"

echo ""
echo "==============================================="

if [[ $RESULT -eq 0 ]]; then
    echo "✅ 실행 완료!"
else
    echo "❌ 에러 발생! (종료 코드: $RESULT)"
    exit 1
fi

echo "==============================================="
