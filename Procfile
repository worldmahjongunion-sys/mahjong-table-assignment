web: python -c "import db; db.init_db()" && (WEBHOOK_PORT=8081 python webhook_server.py &) && streamlit run app.py --server.port=$PORT --server.address=0.0.0.0 --server.headless=true
