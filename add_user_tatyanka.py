#!/usr/bin/env python3
"""
Скрипт для добавления пользователя "татьянка" в базу данных
Запуск: python add_user_tatyanka.py
"""

import sys
import os

# Добавляем текущую директорию в путь для импорта
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Импортируем функции из app.py
from werkzeug.security import generate_password_hash

# Импортируем конфигурацию и функции из app.py
try:
    from app import get_db_connection, PG_CONFIG
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError as e:
    print(f"Ошибка импорта: {e}")
    print("Убедитесь, что все зависимости установлены: pip install -r requirements.txt")
    sys.exit(1)

def add_user_tatyanka():
    """Добавляет пользователя 'татьянка' в базу данных"""
    try:
        conn = psycopg2.connect(**PG_CONFIG)
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        username = 'tatyanka'
        password = 'tatyanka'
        display_name = 'татьянка'
        password_hash = generate_password_hash(password)
        
        cur.execute("""
            INSERT INTO users (username, password_hash, display_name, email, role, email_verified)
            VALUES (%s, %s, %s, NULL, 'user', TRUE)
            ON CONFLICT (username) DO UPDATE SET
                password_hash = EXCLUDED.password_hash,
                display_name = EXCLUDED.display_name
            RETURNING *;
        """, (username, password_hash, display_name))
        
        user = cur.fetchone()
        conn.commit()
        
        if user:
            print(f"✓ Пользователь '{display_name}' успешно создан/обновлен!")
            print(f"  Логин: {username}")
            print(f"  Пароль: {password}")
            print(f"  ID: {user['id']}")
        else:
            print("Пользователь уже существует и был обновлен")
        
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"✗ Ошибка создания пользователя: {e}")
        import traceback
        traceback.print_exc()
        if 'conn' in locals():
            conn.rollback()
            conn.close()
        return False

if __name__ == "__main__":
    print("Добавление пользователя 'татьянка'...")
    success = add_user_tatyanka()
    sys.exit(0 if success else 1)

