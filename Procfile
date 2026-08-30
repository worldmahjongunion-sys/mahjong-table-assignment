web: python -c "import db; db.init_db()" && streamlit run app.py --server.port=$PORT --server.address=0.0.0.0 --server.headless=true
webhook: WEBHOOK_PORT=$PORT python webhook_server.py
