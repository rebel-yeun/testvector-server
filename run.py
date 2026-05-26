#!/usr/bin/env python3
"""
Entry point for the testvector-server.
  Development:  python3 run.py
  Production:   gunicorn --workers 1 -b 0.0.0.0:5000 'run:app'
"""
from app import create_app


app = create_app()


if __name__ == '__main__':
    cfg = getattr(app, 'config_obj')
    print("=" * 50)
    print("워크로드 실행 시스템 시작")
    print("=" * 50)
    print(f"테스트벡터 루트: {cfg.TESTVECTOR_ROOT}")
    print(f"기본 워크로드 폴더: {cfg.DEFAULT_WORKLOAD_DIR}")
    print(f"접속 주소: http://{cfg.HOST}:{cfg.PORT}")
    print("=" * 50)
    app.run(debug=cfg.DEBUG, host=cfg.HOST, port=cfg.PORT)
