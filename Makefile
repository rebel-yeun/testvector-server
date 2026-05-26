.PHONY: help install run run-dev test clean

help: ## 사용 가능한 명령 목록
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## pip install -r requirements.txt
	pip3 install -r requirements.txt

run: ## 운영 서버 시작 (gunicorn, workers=1 필수)
	gunicorn --workers 1 -b 0.0.0.0:5000 'run:app'

run-dev: ## 개발 서버 시작 (Flask built-in, auto-reload 없음)
	python3 run.py

test: ## pytest 실행
	python3 -m pytest tests/ -v

clean: ## 캐시 파일 삭제
	find . -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true
