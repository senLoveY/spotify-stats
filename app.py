import os
import requests
import base64
from urllib.parse import urlencode
from functools import wraps
import threading
import random
import string
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
import io
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import numpy as np
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False
    print("Matplotlib не установлен. Графики в отчетах будут недоступны.")

from flask import Flask, render_template, session, request, redirect, flash, jsonify, send_file
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.errors import UniqueViolation
import snowflake.connector
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
try:
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False
    print("OpenPyXL не установлен. Экспорт/импорт Excel будет недоступен.")

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY') or os.urandom(24)

app.permanent_session_lifetime = timedelta(hours=1)

@app.before_request
def make_session_permanent():
    session.permanent = True

@app.context_processor
def inject_user():
    user = None
    preferences = None
    if 'user_db_id' in session:
        user = get_user_by_id(session['user_db_id'])
        if user:
            preferences = get_user_preferences(user['id'])
    return dict(current_user=user, user_preferences=preferences)

# ==========================================
# 1. КОНФИГУРАЦИЯ (из переменных окружения)
# ==========================================

# Spotify API
CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET')
REDIRECT_URI = os.getenv('SPOTIFY_REDIRECT_URI', 'http://127.0.0.1:5000/callback')
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE_URL = "https://api.spotify.com/v1/"
SCOPE = os.getenv('SPOTIFY_SCOPE', 'user-read-recently-played user-top-read')

# PostgreSQL
PG_CONFIG = {
    'dbname': os.getenv('POSTGRES_DBNAME', 'spotify_project'),
    'user': os.getenv('POSTGRES_USER', 'postgres'),
    'password': os.getenv('POSTGRES_PASSWORD'),
    'host': os.getenv('POSTGRES_HOST', 'localhost'),
    'port': os.getenv('POSTGRES_PORT', '5432')
}

# Snowflake
SNOWFLAKE_CONFIG = {
    'user': os.getenv('SNOWFLAKE_USER'),
    'password': os.getenv('SNOWFLAKE_PASSWORD'),
    'account': os.getenv('SNOWFLAKE_ACCOUNT'),
    'warehouse': os.getenv('SNOWFLAKE_WAREHOUSE', 'COMPUTE_WH'),
    'database': os.getenv('SNOWFLAKE_DATABASE', 'SPOTIFY_DB'),
    'schema': os.getenv('SNOWFLAKE_SCHEMA', 'PUBLIC')
}


def log_snowflake(message: str) -> None:
    """Простой логгер для операций Snowflake (выводит в терминал)."""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[Snowflake] {ts} - {message}")
    except Exception:
        # Никогда не ломаем основной код из‑за логгера
        pass

# Email конфигурация для 2FA
EMAIL_CONFIG = {
    'smtp_server': os.getenv('SMTP_SERVER', 'smtp.gmail.com'),
    'smtp_port': int(os.getenv('SMTP_PORT', '587')),
    'smtp_user': os.getenv('SMTP_USER', ''),
    'smtp_password': os.getenv('SMTP_PASSWORD', ''),
    'from_email': os.getenv('FROM_EMAIL', ''),
    'from_name': os.getenv('FROM_NAME', 'Spotify Analytics')
}

# ==========================================
# 2. ФУНКЦИИ SQL (POSTGRES)
# ==========================================

def get_db_connection():
    return psycopg2.connect(**PG_CONFIG)

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                                                         id SERIAL PRIMARY KEY,
                                                         username VARCHAR(100) UNIQUE,
                        password_hash VARCHAR(255) DEFAULT NULL,
                        email VARCHAR(255) UNIQUE DEFAULT NULL,
                        spotify_id VARCHAR(100) UNIQUE DEFAULT NULL,
                        display_name VARCHAR(150) DEFAULT NULL,
                        role VARCHAR(20) DEFAULT 'user',
                        is_blocked BOOLEAN DEFAULT FALSE,
                        email_verified BOOLEAN DEFAULT FALSE,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        );
                    """)
        
        # Добавляем колонку email, если её нет
        try:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255) UNIQUE")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT FALSE")
        except Exception:
            pass
        
        cur.execute("""
                    CREATE TABLE IF NOT EXISTS verification_codes (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE DEFAULT NULL,
                        email VARCHAR(255) NOT NULL,
                        code VARCHAR(6) NOT NULL,
                        purpose VARCHAR(50) NOT NULL DEFAULT 'registration',
                        expires_at TIMESTAMP NOT NULL,
                        used BOOLEAN DEFAULT FALSE,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT chk_code_expires_after_created CHECK (expires_at > created_at),
                        CONSTRAINT chk_code_length CHECK (LENGTH(code) = 6)
                        );
                    """)
        
        # Таблица для токенов восстановления пароля
        cur.execute("""
                    CREATE TABLE IF NOT EXISTS password_reset_tokens (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE NOT NULL,
                        token VARCHAR(255) UNIQUE NOT NULL,
                        expires_at TIMESTAMP NOT NULL,
                        used BOOLEAN DEFAULT FALSE,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT chk_token_expires_after_created CHECK (expires_at > created_at)
                        );
                    """)
        cur.execute("""
                    CREATE TABLE IF NOT EXISTS audit_logs (
                                                              id SERIAL PRIMARY KEY,
                        user_id INTEGER REFERENCES users(id) DEFAULT NULL,
                        action VARCHAR(100) NOT NULL,
                        details TEXT DEFAULT NULL,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        );
                    """)
        
        cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_sessions (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                        session_token VARCHAR(255) UNIQUE NOT NULL,
                        ip_address VARCHAR(45) DEFAULT NULL,
                        user_agent TEXT DEFAULT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        expires_at TIMESTAMP NOT NULL,
                        is_active BOOLEAN DEFAULT TRUE,
                        CONSTRAINT chk_session_expires_after_created CHECK (expires_at > created_at)
                    );
                    """)
        
        cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_preferences (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE UNIQUE,
                        language VARCHAR(10) DEFAULT 'ru' CHECK (language IN ('ru', 'en', 'uk')),
                        notifications_enabled BOOLEAN DEFAULT TRUE,
                        email_notifications BOOLEAN DEFAULT TRUE,
                        default_view VARCHAR(50) DEFAULT 'dashboard' CHECK (default_view IN ('dashboard', 'recommendations', 'profile')),
                        timezone VARCHAR(50) DEFAULT 'UTC',
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    """)
        
        cur.execute("""
                    CREATE TABLE IF NOT EXISTS feedback (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL DEFAULT NULL,
                        feedback_type VARCHAR(50) NOT NULL DEFAULT 'other' CHECK (feedback_type IN ('bug', 'feature_request', 'complaint', 'praise', 'other')),
                        title VARCHAR(255) NOT NULL,
                        message TEXT NOT NULL,
                        page_url VARCHAR(500) DEFAULT NULL,
                        status VARCHAR(20) DEFAULT 'new' CHECK (status IN ('new', 'reviewed', 'in_progress', 'resolved', 'rejected')),
                        priority VARCHAR(10) DEFAULT NULL CHECK (priority IN ('low', 'medium', 'high', 'critical')),
                        admin_notes TEXT DEFAULT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT chk_feedback_updated_after_created CHECK (updated_at >= created_at),
                        CONSTRAINT chk_feedback_title_length CHECK (LENGTH(title) > 0 AND LENGTH(title) <= 255),
                        CONSTRAINT chk_feedback_message_length CHECK (LENGTH(message) > 0)
                    );
                    """)
        
        cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_user_id ON feedback(user_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback(status);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_type ON feedback(feedback_type);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_created_at ON feedback(created_at DESC);")
        
        # ==========================================
        # CHECK ОГРАНИЧЕНИЯ
        # ==========================================
        
        try:
            # Проверяем, существует ли ограничение, перед добавлением
            cur.execute("""
                SELECT constraint_name 
                FROM information_schema.table_constraints 
                WHERE table_name = 'users' AND constraint_name = 'chk_users_role';
            """)
            if not cur.fetchone():
                cur.execute("""
                    ALTER TABLE users 
                    ADD CONSTRAINT chk_users_role 
                    CHECK (role IN ('user', 'admin'));
                """)
            
            cur.execute("""
                SELECT constraint_name 
                FROM information_schema.table_constraints 
                WHERE table_name = 'users' AND constraint_name = 'chk_users_email_format';
            """)
            if not cur.fetchone():
                cur.execute(r"""
                    ALTER TABLE users 
                    ADD CONSTRAINT chk_users_email_format 
                    CHECK (email IS NULL OR email ~* '^[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,}|localhost)$');
                """)
            else:
                cur.execute("""
                    ALTER TABLE users 
                    DROP CONSTRAINT chk_users_email_format;
                """)
                cur.execute(r"""
                    ALTER TABLE users 
                    ADD CONSTRAINT chk_users_email_format 
                    CHECK (email IS NULL OR email ~* '^[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,}|localhost)$');
                """)
        except Exception as e:
            print(f"Warning: Could not add CHECK constraints to users: {e}")
        
        # ==========================================
        # HISTORY ТАБЛИЦЫ
        # ==========================================
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users_history (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                operation_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                operation_type VARCHAR(10) NOT NULL CHECK (operation_type IN ('INSERT', 'UPDATE', 'DELETE')),
                old_value JSONB DEFAULT NULL,
                new_value JSONB DEFAULT NULL,
                username VARCHAR(100) DEFAULT NULL,
                changed_by_user_id INTEGER REFERENCES users(id) DEFAULT NULL
            );
        """)
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS feedback_history (
                id SERIAL PRIMARY KEY,
                feedback_id INTEGER,
                operation_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                operation_type VARCHAR(10) NOT NULL CHECK (operation_type IN ('INSERT', 'UPDATE', 'DELETE')),
                old_value JSONB DEFAULT NULL,
                new_value JSONB DEFAULT NULL,
                username VARCHAR(100) DEFAULT NULL,
                changed_by_user_id INTEGER REFERENCES users(id) DEFAULT NULL
            );
        """)
        
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_history_user_id ON users_history(user_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_history_operation_date ON users_history(operation_date DESC);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_history_feedback_id ON feedback_history(feedback_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_history_operation_date ON feedback_history(operation_date DESC);")
        
        # ==========================================
        # ТРИГГЕРЫ ДЛЯ HISTORY ТАБЛИЦ
        # ==========================================
        
        cur.execute("""
            CREATE OR REPLACE FUNCTION log_users_changes()
            RETURNS TRIGGER AS $$
            DECLARE
                current_username VARCHAR(100);
                current_user_id INTEGER;
            BEGIN
                current_user_id := current_setting('app.current_user_id', TRUE)::INTEGER;
                SELECT username INTO current_username FROM users WHERE id = current_user_id;
                
                IF TG_OP = 'INSERT' THEN
                    INSERT INTO users_history (user_id, operation_type, new_value, username, changed_by_user_id)
                    VALUES (NEW.id, 'INSERT', row_to_json(NEW)::jsonb, current_username, current_user_id);
                    RETURN NEW;
                ELSIF TG_OP = 'UPDATE' THEN
                    INSERT INTO users_history (user_id, operation_type, old_value, new_value, username, changed_by_user_id)
                    VALUES (NEW.id, 'UPDATE', row_to_json(OLD)::jsonb, row_to_json(NEW)::jsonb, current_username, current_user_id);
                    RETURN NEW;
                ELSIF TG_OP = 'DELETE' THEN
                    INSERT INTO users_history (user_id, operation_type, old_value, username, changed_by_user_id)
                    VALUES (OLD.id, 'DELETE', row_to_json(OLD)::jsonb, current_username, current_user_id);
                    RETURN OLD;
                END IF;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
        """)
        
        # Триггер для users
        cur.execute("""
            DROP TRIGGER IF EXISTS trigger_users_history ON users;
            CREATE TRIGGER trigger_users_history
            AFTER INSERT OR UPDATE OR DELETE ON users
            FOR EACH ROW EXECUTE FUNCTION log_users_changes();
        """)
        
        # Функция для логирования изменений в feedback
        cur.execute("""
            CREATE OR REPLACE FUNCTION log_feedback_changes()
            RETURNS TRIGGER AS $$
            DECLARE
                current_username VARCHAR(100);
                current_user_id INTEGER;
            BEGIN
                current_user_id := current_setting('app.current_user_id', TRUE)::INTEGER;
                SELECT username INTO current_username FROM users WHERE id = current_user_id;
                
                IF TG_OP = 'INSERT' THEN
                    INSERT INTO feedback_history (feedback_id, operation_type, new_value, username, changed_by_user_id)
                    VALUES (NEW.id, 'INSERT', row_to_json(NEW)::jsonb, current_username, current_user_id);
                    RETURN NEW;
                ELSIF TG_OP = 'UPDATE' THEN
                    INSERT INTO feedback_history (feedback_id, operation_type, old_value, new_value, username, changed_by_user_id)
                    VALUES (NEW.id, 'UPDATE', row_to_json(OLD)::jsonb, row_to_json(NEW)::jsonb, current_username, current_user_id);
                    RETURN NEW;
                ELSIF TG_OP = 'DELETE' THEN
                    INSERT INTO feedback_history (feedback_id, operation_type, old_value, username, changed_by_user_id)
                    VALUES (OLD.id, 'DELETE', row_to_json(OLD)::jsonb, current_username, current_user_id);
                    RETURN OLD;
                END IF;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
        """)
        
        # Триггер для feedback
        cur.execute("""
            DROP TRIGGER IF EXISTS trigger_feedback_history ON feedback;
            CREATE TRIGGER trigger_feedback_history
            AFTER INSERT OR UPDATE OR DELETE ON feedback
            FOR EACH ROW EXECUTE FUNCTION log_feedback_changes();
        """)
        
        # ==========================================
        # VIEW ПРЕДСТАВЛЕНИЯ
        # ==========================================
        
        # VIEW: Активные пользователи с информацией о сессиях
        cur.execute("""
            CREATE OR REPLACE VIEW v_active_users_with_sessions AS
            SELECT 
                u.id,
                u.username,
                u.display_name,
                u.email,
                u.role,
                u.is_blocked,
                u.email_verified,
                u.created_at,
                COUNT(DISTINCT s.id) as active_sessions_count,
                MAX(s.created_at) as last_session_date,
                MAX(s.expires_at) as latest_session_expires
            FROM users u
            LEFT JOIN user_sessions s ON u.id = s.user_id AND s.is_active = TRUE
            WHERE u.is_blocked = FALSE
            GROUP BY u.id, u.username, u.display_name, u.email, u.role, u.is_blocked, u.email_verified, u.created_at;
        """)
        
        # VIEW: Feedback с информацией о пользователях и статистикой
        cur.execute("""
            CREATE OR REPLACE VIEW v_feedback_with_users AS
            SELECT 
                f.id,
                f.feedback_type,
                f.title,
                f.message,
                f.status,
                f.priority,
                f.created_at,
                f.updated_at,
                u.id as user_id,
                u.username,
                u.display_name,
                u.email,
                u.role as user_role,
                CASE 
                    WHEN f.status = 'new' THEN 1
                    WHEN f.status = 'reviewed' THEN 2
                    WHEN f.status = 'in_progress' THEN 3
                    WHEN f.status = 'resolved' THEN 4
                    WHEN f.status = 'rejected' THEN 5
                    ELSE 0
                END as status_order
            FROM feedback f
            LEFT JOIN users u ON f.user_id = u.id
            ORDER BY f.created_at DESC;
        """)
        
        # VIEW: Статистика по пользователям
        cur.execute("""
            CREATE OR REPLACE VIEW v_user_statistics AS
            SELECT 
                u.id,
                u.username,
                u.display_name,
                u.role,
                u.created_at as registration_date,
                COUNT(DISTINCT s.id) as total_sessions,
                COUNT(DISTINCT CASE WHEN s.is_active = TRUE THEN s.id END) as active_sessions,
                COUNT(DISTINCT f.id) as feedback_count,
                COUNT(DISTINCT CASE WHEN f.status = 'new' THEN f.id END) as new_feedback_count,
                COUNT(DISTINCT al.id) as audit_logs_count,
                MAX(s.created_at) as last_login_date
            FROM users u
            LEFT JOIN user_sessions s ON u.id = s.user_id
            LEFT JOIN feedback f ON u.id = f.user_id
            LEFT JOIN audit_logs al ON u.id = al.user_id
            GROUP BY u.id, u.username, u.display_name, u.role, u.created_at;
        """)
        
        # VIEW: Последние действия пользователей (объединяет audit_logs и user_sessions)
        cur.execute("""
            CREATE OR REPLACE VIEW v_recent_user_activity AS
            SELECT 
                'session' as activity_type,
                s.id as activity_id,
                s.user_id,
                u.username,
                u.display_name,
                CASE 
                    WHEN s.is_active = TRUE THEN 'Вход в систему'
                    ELSE 'Выход из системы'
                END as action_description,
                s.created_at as activity_date,
                s.ip_address,
                s.user_agent
            FROM user_sessions s
            JOIN users u ON s.user_id = u.id
            UNION ALL
            SELECT 
                'audit' as activity_type,
                al.id as activity_id,
                al.user_id,
                u.username,
                u.display_name,
                al.action as action_description,
                al.timestamp as activity_date,
                NULL as ip_address,
                NULL as user_agent
            FROM audit_logs al
            JOIN users u ON al.user_id = u.id
            ORDER BY activity_date DESC
            LIMIT 1000;
        """)
        
        # Создаем админа по умолчанию (логин: admin, пароль: admin)
        admin_pass = generate_password_hash("admin")
        admin_email = os.getenv('ADMIN_EMAIL', 'admin@localhost')
        cur.execute("""
                    INSERT INTO users (username, password_hash, display_name, email, role, email_verified)
                    VALUES ('admin', %s, 'Super Admin', %s, 'admin', TRUE)
                        ON CONFLICT (username) DO NOTHING;
                    """, (admin_pass, admin_email))
        
        # Создаем пользователя татьянка (логин: tatyanka, пароль: tatyanka)
        tatyanka_pass = generate_password_hash("tatyanka")
        cur.execute("""
                    INSERT INTO users (username, password_hash, display_name, email, role, email_verified)
                    VALUES ('tatyanka', %s, 'татьянка', NULL, 'user', TRUE)
                        ON CONFLICT (username) DO NOTHING;
                    """, (tatyanka_pass,))

        conn.commit()
        print("База данных инициализирована.")
    except Exception as e:
        print(f"Ошибка БД: {e}")
    finally:
        cur.close()
        conn.close()

def get_user_by_id(user_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        return cur.fetchone()
    finally:
        cur.close()
        conn.close()

def get_user_by_username(username):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        return cur.fetchone()
    finally:
        cur.close()
        conn.close()

def get_user_by_spotify_id(spotify_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM users WHERE spotify_id = %s", (spotify_id,))
        return cur.fetchone()
    finally:
        cur.close()
        conn.close()

def create_local_user(username, password, display_name, email):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        p_hash = generate_password_hash(password)
        cur.execute("""
                    INSERT INTO users (username, password_hash, display_name, email, role, email_verified)
                    VALUES (%s, %s, %s, %s, 'user', FALSE)
                        RETURNING *;
                    """, (username, p_hash, display_name, email))
        user = cur.fetchone()
        conn.commit()
        return user
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cur.close()
        conn.close()

def generate_verification_code(length=6):
    """Генерирует случайный код подтверждения"""
    return ''.join(random.choices(string.digits, k=length))

def generate_chart_image(chart_type, data, title, xlabel='', ylabel=''):
    """
    Генерирует изображение графика и возвращает его в base64
    
    chart_type: 'bar', 'line', 'doughnut', 'pie'
    data: список словарей с данными
    """
    if not MATPLOTLIB_AVAILABLE:
        return None
    
    try:
        plt.figure(figsize=(10, 6))
        plt.rcParams['font.size'] = 10
        plt.rcParams['axes.facecolor'] = 'white'
        plt.rcParams['figure.facecolor'] = 'white'
        
        # Цветовая палитра Spotify
        spotify_colors = ['#1DB954', '#1ed760', '#00f2fe', '#4facfe', '#667eea', 
                         '#764ba2', '#f093fb', '#FF6384', '#36A2EB', '#FFCE56']
        
        if chart_type == 'doughnut' or chart_type == 'pie':
            # Круговая диаграмма
            labels = [item.get('genre', 'Не определен') for item in data]
            values = []
            for item in data:
                try:
                    val = int(item.get('count', 0)) if item.get('count') is not None else 0
                except (ValueError, TypeError):
                    val = 0
                values.append(val)
            
            # Убираем пустые значения
            filtered_data = [(l, v) for l, v in zip(labels, values) if v > 0]
            if not filtered_data:
                plt.close()
                return None
            
            labels, values = zip(*filtered_data)
            
            # Используем цвета Spotify, циклически повторяя палитру
            colors = [spotify_colors[i % len(spotify_colors)] for i in range(len(labels))]
            plt.pie(values, labels=labels, autopct='%1.1f%%', startangle=90, colors=colors, 
                   textprops={'fontsize': 10, 'fontweight': 'bold'})
            plt.title(title, fontsize=14, fontweight='bold', pad=20, color='#1DB954')
            
        elif chart_type == 'bar':
            # Столбчатая диаграмма
            x_data = []
            y_data = []
            for item in data:
                try:
                    x_val = int(item.get('hour', 0)) if item.get('hour') is not None else 0
                    y_val = int(item.get('count', 0)) if item.get('count') is not None else 0
                except (ValueError, TypeError):
                    continue
                x_data.append(x_val)
                y_data.append(y_val)
            
            if not x_data:
                plt.close()
                return None
            
            # Столбчатая диаграмма с цветами Spotify
            bars = plt.bar(x_data, y_data, color='#1DB954', alpha=0.85, edgecolor='#1ed760', linewidth=1.2)
            
            plt.xlabel(xlabel, fontsize=12, color='#333')
            plt.ylabel(ylabel, fontsize=12, color='#333')
            plt.title(title, fontsize=14, fontweight='bold', pad=20, color='#1DB954')
            plt.xticks(range(0, 24, 2))
            plt.grid(axis='y', alpha=0.2, linestyle='--', color='#1DB954')
            plt.gca().spines['top'].set_visible(False)
            plt.gca().spines['right'].set_visible(False)
            plt.gca().spines['left'].set_color('#1DB954')
            plt.gca().spines['bottom'].set_color('#1DB954')
            
        elif chart_type == 'line' or chart_type == 'weekday':
            # График для дней недели - используем столбчатую диаграмму для лучшей наглядности
            weekday_names = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
            weekday_short = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
            
            # Маппинг строковых названий дней на числа
            weekday_str_to_num = {
                'Пн': 1, 'Понедельник': 1, 'Monday': 1,
                'Вт': 2, 'Вторник': 2, 'Tuesday': 2,
                'Ср': 3, 'Среда': 3, 'Wednesday': 3,
                'Чт': 4, 'Четверг': 4, 'Thursday': 4,
                'Пт': 5, 'Пятница': 5, 'Friday': 5,
                'Сб': 6, 'Суббота': 6, 'Saturday': 6,
                'Вс': 7, 'Воскресенье': 7, 'Sunday': 7
            }
            
            # Создаем словарь для всех дней недели
            weekday_dict = {i: 0 for i in range(1, 8)}
            
            # Заполняем данные из входных данных
            for item in data:
                try:
                    weekday_val = item.get('weekday', 1)
                    count = int(item.get('count', 0)) if item.get('count') is not None else 0
                    
                    # Преобразуем weekday в число
                    if isinstance(weekday_val, str):
                        weekday = weekday_str_to_num.get(weekday_val, 1)
                    else:
                        weekday = int(weekday_val) if weekday_val is not None else 1
                except (ValueError, TypeError):
                    continue
                
                if 1 <= weekday <= 7:
                    weekday_dict[weekday] = count
            
            # Сортируем по дням недели (1-7)
            x_data = sorted(weekday_dict.keys())
            y_data = [weekday_dict[d] for d in x_data]
            labels_data = [weekday_short[d - 1] for d in x_data]
            
            if not x_data or sum(y_data) == 0:
                plt.close()
                return None
            
            # Используем столбчатую диаграмму для дней недели
            bars = plt.bar(x_data, y_data, color='#1DB954', alpha=0.85, edgecolor='#1ed760', linewidth=1.5)
            
            # Добавляем значения на столбцы
            for i, (x, y) in enumerate(zip(x_data, y_data)):
                if y > 0:
                    plt.text(x, y + max(y_data) * 0.01, str(y), ha='center', va='bottom', 
                           fontsize=9, fontweight='bold', color='#1DB954')
            
            plt.xlabel(xlabel, fontsize=12, color='#333')
            plt.ylabel(ylabel, fontsize=12, color='#333')
            plt.title(title, fontsize=14, fontweight='bold', pad=20, color='#1DB954')
            plt.xticks(x_data, labels_data)
            plt.grid(axis='y', alpha=0.2, linestyle='--', color='#1DB954')
            plt.ylim(bottom=0, top=max(y_data) * 1.15 if max(y_data) > 0 else 10)
            plt.gca().spines['top'].set_visible(False)
            plt.gca().spines['right'].set_visible(False)
            plt.gca().spines['left'].set_color('#1DB954')
            plt.gca().spines['bottom'].set_color('#1DB954')
        
        plt.tight_layout()
        
        # Сохраняем в буфер памяти
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        buf.seek(0)
        image_base64 = base64.b64encode(buf.read()).decode('utf-8')
        buf.close()
        plt.close()
        
        return image_base64
    except Exception as e:
        print(f"Ошибка генерации графика: {e}")
        import traceback
        traceback.print_exc()
        plt.close()
        return None

def generate_dashboard_report(
    user,
    recent_tracks,
    top_tracks,
    genre_distribution,
    activity_by_hour,
    activity_by_weekday,
    user_vibe,
    top_artists=None,
    summary=None,
):
    """Генерирует HTML отчет на основе данных дашборда (вся ключевая информация)."""
    from datetime import datetime
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            body {{
                font-family: Arial, sans-serif;
                line-height: 1.6;
                color: #333;
                max-width: 800px;
                margin: 0 auto;
                padding: 20px;
                background-color: #f5f5f5;
            }}
            .header {{
                background: linear-gradient(135deg, #1DB954 0%, #1ed760 100%);
                color: white;
                padding: 30px;
                border-radius: 10px;
                margin-bottom: 30px;
                text-align: center;
            }}
            .section {{
                background: white;
                padding: 20px;
                margin-bottom: 20px;
                border-radius: 8px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }}
            .section h2 {{
                color: #1DB954;
                border-bottom: 2px solid #1DB954;
                padding-bottom: 10px;
                margin-bottom: 15px;
            }}
            .track-item {{
                padding: 10px;
                border-bottom: 1px solid #eee;
                display: flex;
                justify-content: space-between;
            }}
            .track-item:last-child {{
                border-bottom: none;
            }}
            .track-name {{
                font-weight: bold;
                color: #333;
            }}
            .track-artist {{
                color: #666;
                font-size: 0.9em;
            }}
            .vibe-box {{
                background: linear-gradient(135deg, #667eea 0%, #764ba2 25%, #f093fb 50%, #4facfe 75%, #00f2fe 100%);
                color: white;
                padding: 20px;
                border-radius: 10px;
                text-align: center;
                margin-bottom: 20px;
            }}
            .stats-table {{
                width: 100%;
                border-collapse: collapse;
                margin-top: 10px;
            }}
            .stats-table th, .stats-table td {{
                padding: 10px;
                text-align: left;
                border-bottom: 1px solid #eee;
            }}
            .stats-table th {{
                background-color: #1DB954;
                color: white;
            }}
            .footer {{
                text-align: center;
                color: #666;
                font-size: 0.9em;
                margin-top: 30px;
                padding-top: 20px;
                border-top: 1px solid #eee;
            }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>Отчет Spotify Analytics</h1>
            <p>Пользователь: {user.get('display_name', 'Неизвестно')}</p>
            <p>Дата: {datetime.now().strftime('%d.%m.%Y %H:%M')}</p>
        </div>
    """
    
    # Сводные показатели
    if summary:
        total_minutes = summary.get("total_minutes_listened") or 0
        favorite_genre = summary.get("favorite_genre") or "—"
        favorite_artist = summary.get("favorite_artist") or "—"
        favorite_track = summary.get("favorite_track") or "—"

        html += f"""
        <div class="section">
            <h2>Сводные показатели</h2>
            <table class="stats-table">
                <tbody>
                    <tr>
                        <th style="width: 40%;">Минут с музыкой</th>
                        <td>{total_minutes}</td>
                    </tr>
                    <tr>
                        <th>Любимый жанр</th>
                        <td>{favorite_genre}</td>
                    </tr>
                    <tr>
                        <th>Любимый исполнитель</th>
                        <td>{favorite_artist}</td>
                    </tr>
                    <tr>
                        <th>Любимый трек</th>
                        <td>{favorite_track}</td>
                    </tr>
                </tbody>
            </table>
        </div>
        """
    
    # Музыкальный вайб
    if user_vibe:
        html += f"""
        <div class="vibe-box">
            <h3 style="margin: 0 0 10px 0;">Твой музыкальный вайб</h3>
            <p style="font-size: 1.5em; font-weight: bold; margin: 0;">{user_vibe}</p>
        </div>
        """
    
    # Недавние прослушивания
    if recent_tracks:
        html += """
        <div class="section">
            <h2>Недавние прослушивания (последние 5)</h2>
        """
        for i, item in enumerate(recent_tracks[:5], 1):
            track = item.get('track', {})
            track_name = track.get('name', 'Неизвестно')
            artist_name = track.get('artists', [{}])[0].get('name', 'Неизвестный артист') if track.get('artists') else 'Неизвестный артист'
            html += f"""
            <div class="track-item">
                <div>
                    <div class="track-name">{i}. {track_name}</div>
                    <div class="track-artist">{artist_name}</div>
                </div>
            </div>
            """
        html += "</div>"
    
    # Топ-5 треков
    if top_tracks:
        html += """
        <div class="section">
            <h2>Топ-5 самых прослушиваемых треков</h2>
        """
        for i, track in enumerate(top_tracks[:5], 1):
            track_name = track.get('name', 'Неизвестно')
            artist_name = track.get('artist', 'Неизвестный артист')
            count = track.get('count', 0)
            # Преобразуем в int, если это строка
            try:
                count = int(count) if count is not None else 0
            except (ValueError, TypeError):
                count = 0
            html += f"""
                    <div class="track-item">
                        <div>
                            <div class="track-name">{i}. {track_name}</div>
                            <div class="track-artist">{artist_name}</div>
                        </div>
                        <div style="color: #1DB954; font-weight: bold;">{count} раз</div>
                    </div>
            """
        html += "</div>"
    
    # Топ-артисты
    if top_artists:
        html += """
        <div class="section">
            <h2>Топ-исполнители</h2>
            <table class="stats-table">
                <thead>
                    <tr>
                        <th>Исполнитель</th>
                        <th>Прослушиваний</th>
                    </tr>
                </thead>
                <tbody>
        """
        for artist in top_artists[:10]:
            name = artist.get("artist", "Неизвестный исполнитель")
            count = artist.get("count", 0)
            try:
                count = int(count) if count is not None else 0
            except (ValueError, TypeError):
                count = 0
            html += f"""
                    <tr>
                        <td>{name}</td>
                        <td>{count}</td>
                    </tr>
            """
        html += """
                </tbody>
            </table>
        </div>
        """
    
    # Анализ жанров (график)
    if genre_distribution:
        chart_image = generate_chart_image('doughnut', genre_distribution, 'Анализ жанров')
        if chart_image:
            html += f"""
        <div class="section">
            <h2>Анализ жанров</h2>
            <div style="text-align: center; margin: 20px 0;">
                <img src="data:image/png;base64,{chart_image}" alt="Анализ жанров" style="max-width: 100%; height: auto; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1);">
            </div>
        </div>
        """
        else:
            # Fallback на таблицу, если график не удалось создать
            html += """
        <div class="section">
            <h2>Анализ жанров</h2>
            <table class="stats-table">
                <thead>
                    <tr>
                        <th>Жанр</th>
                        <th>Количество прослушиваний</th>
                    </tr>
                </thead>
                <tbody>
        """
            for genre_data in genre_distribution:
                genre = genre_data.get('genre', 'Не определен')
                count = genre_data.get('count', 0)
                try:
                    count = int(count) if count is not None else 0
                except (ValueError, TypeError):
                    count = 0
                html += f"""
                    <tr>
                        <td>{genre}</td>
                        <td>{count}</td>
                    </tr>
            """
            html += """
                </tbody>
            </table>
        </div>
        """
    
    # Прослушивания по часам (график)
    if activity_by_hour:
        chart_image = generate_chart_image('bar', activity_by_hour, 'Прослушивания по часам дня', 'Час дня', 'Количество прослушиваний')
        if chart_image:
            html += f"""
        <div class="section">
            <h2>Прослушивания по часам дня</h2>
            <div style="text-align: center; margin: 20px 0;">
                <img src="data:image/png;base64,{chart_image}" alt="Прослушивания по часам" style="max-width: 100%; height: auto; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1);">
            </div>
        </div>
        """
        else:
            # Fallback на таблицу
            html += """
        <div class="section">
            <h2>Прослушивания по часам дня</h2>
            <table class="stats-table">
                <thead>
                    <tr>
                        <th>Час</th>
                        <th>Количество прослушиваний</th>
                    </tr>
                </thead>
                <tbody>
        """
            for hour_data in activity_by_hour:
                hour = hour_data.get('hour', 0)
                count = hour_data.get('count', 0)
                try:
                    hour = int(hour) if hour is not None else 0
                except (ValueError, TypeError):
                    hour = 0
                try:
                    count = int(count) if count is not None else 0
                except (ValueError, TypeError):
                    count = 0
                html += f"""
                    <tr>
                        <td>{hour}:00</td>
                        <td>{count}</td>
                    </tr>
            """
            html += """
                </tbody>
            </table>
        </div>
        """
    
    # Прослушивания по дням недели (график)
    if activity_by_weekday:
        chart_image = generate_chart_image('weekday', activity_by_weekday, 'Прослушивания по дням недели', 'День недели', 'Количество прослушиваний')
        if chart_image:
            html += f"""
        <div class="section">
            <h2>Прослушивания по дням недели</h2>
            <div style="text-align: center; margin: 20px 0;">
                <img src="data:image/png;base64,{chart_image}" alt="Прослушивания по дням недели" style="max-width: 100%; height: auto; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1);">
            </div>
        </div>
        """
        else:
            # Fallback на таблицу
            weekday_names = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
            html += """
        <div class="section">
            <h2>Прослушивания по дням недели</h2>
            <table class="stats-table">
                <thead>
                    <tr>
                        <th>День недели</th>
                        <th>Количество прослушиваний</th>
                    </tr>
                </thead>
                <tbody>
        """
            for weekday_data in activity_by_weekday:
                weekday = weekday_data.get('weekday', 1)
                count = weekday_data.get('count', 0)
                try:
                    weekday = int(weekday) if weekday is not None else 1
                except (ValueError, TypeError):
                    weekday = 1
                try:
                    count = int(count) if count is not None else 0
                except (ValueError, TypeError):
                    count = 0
                weekday_name = weekday_names[weekday - 1] if 1 <= weekday <= 7 else f'День {weekday}'
                html += f"""
                    <tr>
                        <td>{weekday_name}</td>
                        <td>{count}</td>
                    </tr>
            """
            html += """
                </tbody>
            </table>
        </div>
        """
    
    html += f"""
        <div class="footer">
            <p>Сгенерировано автоматически системой Spotify Analytics</p>
            <p>{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</p>
        </div>
    </body>
    </html>
    """
    
    return html

def send_dashboard_report_email(
    user_email,
    user,
    recent_tracks,
    top_tracks,
    genre_distribution,
    activity_by_hour,
    activity_by_weekday,
    user_vibe,
    top_artists=None,
    summary=None,
):
    """Отправляет отчет дашборда на email. Возвращает (success: bool, message: str)"""
    # Проверяем наличие email у пользователя
    if not user_email:
        return False, "У вас не указан email адрес. Пожалуйста, укажите email в профиле."
    
    # Проверяем настройки SMTP
    if not EMAIL_CONFIG['smtp_user'] or not EMAIL_CONFIG['smtp_password']:
        error_msg = "Email не настроен на сервере. Обратитесь к администратору."
        print(f"Email не настроен. Невозможно отправить отчет на {user_email}")
        return False, error_msg
    
    if not EMAIL_CONFIG['smtp_server']:
        error_msg = "SMTP сервер не настроен. Обратитесь к администратору."
        print(f"SMTP сервер не настроен")
        return False, error_msg
    
    try:
        msg = MIMEMultipart()
        msg['From'] = f"{EMAIL_CONFIG['from_name']} <{EMAIL_CONFIG['from_email']}>"
        msg['To'] = user_email
        msg['Subject'] = f"Отчет Spotify Analytics - {datetime.now().strftime('%d.%m.%Y')}"
        
        # Генерируем HTML отчет
        html_content = generate_dashboard_report(
            user,
            recent_tracks,
            top_tracks,
            genre_distribution,
            activity_by_hour,
            activity_by_weekday,
            user_vibe,
            top_artists=top_artists,
            summary=summary,
        )
        
        msg.attach(MIMEText(html_content, 'html'))
        
        # Подключаемся к SMTP серверу
        try:
            server = smtplib.SMTP(EMAIL_CONFIG['smtp_server'], EMAIL_CONFIG['smtp_port'], timeout=10)
        except Exception as e:
            error_msg = f"Не удалось подключиться к SMTP серверу {EMAIL_CONFIG['smtp_server']}:{EMAIL_CONFIG['smtp_port']}. Проверьте настройки."
            print(f"Ошибка подключения к SMTP: {e}")
            return False, error_msg
        
        try:
            server.starttls()
        except Exception as e:
            error_msg = f"Ошибка при установке TLS соединения: {str(e)}"
            print(f"Ошибка TLS: {e}")
            server.quit()
            return False, error_msg
        
        try:
            server.login(EMAIL_CONFIG['smtp_user'], EMAIL_CONFIG['smtp_password'])
        except smtplib.SMTPAuthenticationError as e:
            error_msg = "Ошибка аутентификации SMTP. Проверьте логин и пароль в настройках."
            print(f"Ошибка аутентификации SMTP: {e}")
            server.quit()
            return False, error_msg
        
        try:
            server.send_message(msg)
        except Exception as e:
            error_msg = f"Ошибка при отправке сообщения: {str(e)}"
            print(f"Ошибка отправки сообщения: {e}")
            server.quit()
            return False, error_msg
        
        server.quit()
        return True, "Отчет успешно отправлен на ваш email"
        
    except Exception as e:
        error_msg = f"Неожиданная ошибка при отправке отчета: {str(e)}"
        print(f"Ошибка отправки отчета на email: {e}")
        import traceback
        traceback.print_exc()
        return False, error_msg

def send_verification_email(email, code, purpose='registration'):
    """Отправляет код подтверждения на email"""
    # Проверяем, что настройки не являются заглушками
    if (not EMAIL_CONFIG['smtp_user'] or 
        not EMAIL_CONFIG['smtp_password'] or 
        not EMAIL_CONFIG['from_email'] or
        'your_email' in EMAIL_CONFIG['smtp_user'].lower() or
        'your_app_password' in EMAIL_CONFIG['smtp_password'].lower() or
        'your_email' in EMAIL_CONFIG['from_email'].lower()):
        print(f"\n{'='*60}")
        print(f"⚠️  EMAIL НЕ НАСТРОЕН!")
        print(f"{'='*60}")
        print(f"Код подтверждения для {email}: {code}")
        print(f"Настройте SMTP в файле .env:")
        print(f"  - SMTP_USER=ваш_email@gmail.com")
        print(f"  - SMTP_PASSWORD=пароль_приложения")
        print(f"  - FROM_EMAIL=ваш_email@gmail.com")
        print(f"{'='*60}\n")
        return False
    
    try:
        subject_map = {
            'registration': 'Код подтверждения регистрации',
            'login': 'Код подтверждения входа',
            'reset': 'Код сброса пароля',
            'email_verification': 'Код подтверждения email'
        }
        
        msg = MIMEMultipart()
        msg['From'] = f"{EMAIL_CONFIG['from_name']} <{EMAIL_CONFIG['from_email']}>"
        msg['To'] = email
        msg['Subject'] = subject_map.get(purpose, 'Код подтверждения')
        
        body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #1DB954;">Spotify Analytics</h2>
                <p>Ваш код подтверждения:</p>
                <div style="background: linear-gradient(135deg, #1DB954 0%, #1ed760 100%); 
                            color: white; padding: 20px; text-align: center; 
                            font-size: 32px; font-weight: bold; border-radius: 10px; 
                            margin: 20px 0; letter-spacing: 5px;">
                    {code}
                </div>
                <p style="color: #666; font-size: 14px;">
                    Код действителен в течение 10 минут. Не передавайте его никому.
                </p>
            </div>
        </body>
        </html>
        """
        
        msg.attach(MIMEText(body, 'html'))
        
        server = smtplib.SMTP(EMAIL_CONFIG['smtp_server'], EMAIL_CONFIG['smtp_port'], timeout=10)
        server.starttls()
        server.login(EMAIL_CONFIG['smtp_user'], EMAIL_CONFIG['smtp_password'])
        server.send_message(msg)
        server.quit()
        
        print(f"✅ Email успешно отправлен на {email}")
        return True
    except smtplib.SMTPConnectError as e:
        print(f"❌ Ошибка подключения к SMTP серверу: {e}")
        print(f"   Проверьте SMTP_SERVER и SMTP_PORT в .env")
        return False
    except smtplib.SMTPAuthenticationError as e:
        print(f"❌ Ошибка аутентификации SMTP: {e}")
        print(f"   Проверьте SMTP_USER и SMTP_PASSWORD в .env")
        print(f"   Для Gmail используйте пароль приложения, а не обычный пароль")
        return False
    except smtplib.SMTPException as e:
        print(f"❌ Ошибка отправки email: {e}")
        return False
    except Exception as e:
        print(f"❌ Неожиданная ошибка при отправке email: {e}")
        import traceback
        traceback.print_exc()
        return False

def save_verification_code(user_id, email, code, purpose='registration', expires_minutes=10):
    """Сохраняет код подтверждения в БД"""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        expires_at = datetime.now() + timedelta(minutes=expires_minutes)
        cur.execute("""
            INSERT INTO verification_codes (user_id, email, code, purpose, expires_at)
            VALUES (%s, %s, %s, %s, %s)
        """, (user_id, email, code, purpose, expires_at))
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cur.close()
        conn.close()

def verify_code(email, code, purpose='registration'):
    """Проверяет код подтверждения"""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT * FROM verification_codes
            WHERE email = %s AND code = %s AND purpose = %s 
            AND used = FALSE AND expires_at > NOW()
            ORDER BY created_at DESC
            LIMIT 1
        """, (email, code, purpose))
        result = cur.fetchone()
        
        if result:
            # Помечаем код как использованный
            cur.execute("""
                UPDATE verification_codes SET used = TRUE WHERE id = %s
            """, (result['id'],))
            conn.commit()
            return True
        return False
    except Exception as e:
        conn.rollback()
        return False
    finally:
        cur.close()
        conn.close()

def generate_password_reset_token(user_id):
    """Генерирует токен для восстановления пароля"""
    import secrets
    token = secrets.token_urlsafe(32)
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        expires_at = datetime.now() + timedelta(hours=1)  # Токен действителен 1 час
        cur.execute("""
            INSERT INTO password_reset_tokens (user_id, token, expires_at)
            VALUES (%s, %s, %s)
            RETURNING token
        """, (user_id, token, expires_at))
        conn.commit()
        return token
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cur.close()
        conn.close()

def verify_password_reset_token(token):
    """Проверяет токен восстановления пароля и возвращает user_id"""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT user_id FROM password_reset_tokens
            WHERE token = %s AND used = FALSE AND expires_at > NOW()
            ORDER BY created_at DESC
            LIMIT 1
        """, (token,))
        result = cur.fetchone()
        
        if result:
            # Помечаем токен как использованный
            cur.execute("""
                UPDATE password_reset_tokens SET used = TRUE WHERE token = %s
            """, (token,))
            conn.commit()
            return result['user_id']
        return None
    except Exception as e:
        conn.rollback()
        return None
    finally:
        cur.close()
        conn.close()

def send_password_reset_email(email, reset_url):
    """Отправляет email со ссылкой для восстановления пароля"""
    if (not EMAIL_CONFIG['smtp_user'] or 
        not EMAIL_CONFIG['smtp_password'] or 
        not EMAIL_CONFIG['from_email'] or
        'your_email' in EMAIL_CONFIG['smtp_user'].lower() or
        'your_app_password' in EMAIL_CONFIG['smtp_password'].lower() or
        'your_email' in EMAIL_CONFIG['from_email'].lower()):
        print(f"\n{'='*60}")
        print(f"⚠️  EMAIL НЕ НАСТРОЕН!")
        print(f"{'='*60}")
        print(f"Ссылка для восстановления пароля: {reset_url}")
        print(f"{'='*60}\n")
        return False
    
    try:
        msg = MIMEMultipart()
        msg['From'] = f"{EMAIL_CONFIG['from_name']} <{EMAIL_CONFIG['from_email']}>"
        msg['To'] = email
        msg['Subject'] = 'Восстановление пароля - Spotify Analytics'
        
        body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #1DB954;">Spotify Analytics</h2>
                <p>Вы запросили восстановление пароля для вашего аккаунта.</p>
                <p>Нажмите на кнопку ниже, чтобы установить новый пароль:</p>
                <div style="text-align: center; margin: 30px 0;">
                    <a href="{reset_url}" 
                       style="background: linear-gradient(135deg, #1DB954 0%, #1ed760 100%); 
                              color: white; padding: 16px 32px; text-decoration: none; 
                              border-radius: 12px; font-weight: bold; display: inline-block;
                              box-shadow: 0 4px 15px rgba(29, 185, 84, 0.3);">
                        Восстановить пароль
                    </a>
                </div>
                <p style="color: #666; font-size: 14px;">
                    Или скопируйте и вставьте эту ссылку в браузер:
                </p>
                <p style="color: #1DB954; font-size: 12px; word-break: break-all;">
                    {reset_url}
                </p>
                <p style="color: #666; font-size: 14px; margin-top: 30px;">
                    Ссылка действительна в течение 1 часа. Если вы не запрашивали восстановление пароля, просто проигнорируйте это письмо.
                </p>
            </div>
        </body>
        </html>
        """
        
        msg.attach(MIMEText(body, 'html'))
        
        server = smtplib.SMTP(EMAIL_CONFIG['smtp_server'], EMAIL_CONFIG['smtp_port'], timeout=10)
        server.starttls()
        server.login(EMAIL_CONFIG['smtp_user'], EMAIL_CONFIG['smtp_password'])
        server.send_message(msg)
        server.quit()
        
        print(f"✅ Email для восстановления пароля успешно отправлен на {email}")
        return True
    except Exception as e:
        print(f"❌ Ошибка отправки email для восстановления пароля: {e}")
        import traceback
        traceback.print_exc()
        return False

def link_spotify_to_user(user_id, spotify_id):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET spotify_id = %s WHERE id = %s", (spotify_id, user_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()

def create_spotify_user(spotify_id, display_name):
    # Создает пользователя, если он зашел через Spotify (без пароля)
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # Генерируем фейковый username, чтобы не было конфликта unique
        fake_username = f"spotify_{spotify_id}"
        cur.execute("""
                    INSERT INTO users (username, spotify_id, display_name, role)
                    VALUES (%s, %s, %s, 'user')
                        RETURNING *;
                    """, (fake_username, spotify_id, display_name))
        user = cur.fetchone()
        conn.commit()
        return user
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cur.close()
        conn.close()

def log_action(user_id, action, details=None):
    """Логирует действие пользователя в audit_logs"""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Устанавливаем переменную сессии для триггеров
        cur.execute("SET LOCAL app.current_user_id = %s", (str(user_id) if user_id else 'NULL',))
        cur.execute("INSERT INTO audit_logs (user_id, action, details) VALUES (%s, %s, %s)", (user_id, action, details))
        conn.commit()
    except: pass
    finally: cur.close(); conn.close()

def get_all_users(search=None, role=None, status=None, sort_by='id', sort_order='asc'):
    """
    Возвращает список пользователей с возможностью фильтрации и сортировки:
    - search: подстрока для поиска по display_name, username, spotify_id
    - role: 'admin' или 'user'
    - status: 'active' или 'blocked'
    - sort_by: поле для сортировки ('id', 'display_name', 'username', 'role', 'is_blocked', 'created_at')
    - sort_order: 'asc' или 'desc'
    """
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        query = "SELECT * FROM users WHERE 1=1"
        params = []

        if search:
            s = f"%{search.lower()}%"
            query += " AND (LOWER(display_name) LIKE %s OR LOWER(username) LIKE %s OR LOWER(COALESCE(spotify_id, '')) LIKE %s)"
            params.extend([s, s, s])

        if role in ("admin", "user"):
            query += " AND role = %s"
            params.append(role)

        if status in ("active", "blocked"):
            query += " AND is_blocked = %s"
            params.append(status == "blocked")

        # Валидация полей сортировки
        valid_sort_fields = ['id', 'display_name', 'username', 'role', 'is_blocked', 'created_at']
        if sort_by not in valid_sort_fields:
            sort_by = 'id'
        
        sort_order = 'DESC' if sort_order.lower() == 'desc' else 'ASC'
        query += f" ORDER BY {sort_by} {sort_order}"
        
        cur.execute(query, tuple(params))
        res = cur.fetchall()
        return res
    finally:
        cur.close()
        conn.close()

def get_recent_logs(limit=50, sort_by='timestamp', sort_order='desc', user_id=None):
    """
    Возвращает логи аудита с возможностью сортировки и фильтрации по пользователю:
    - limit: максимальное количество записей
    - sort_by: поле для сортировки ('timestamp', 'action', 'display_name')
    - sort_order: 'asc' или 'desc'
    - user_id: ID пользователя для фильтрации (если None, возвращает все логи)
    """
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # Валидация полей сортировки
        valid_sort_fields = ['timestamp', 'action', 'display_name']
        if sort_by not in valid_sort_fields:
            sort_by = 'timestamp'
        
        sort_order = 'DESC' if sort_order.lower() == 'desc' else 'ASC'
        
        query = """
                SELECT l.timestamp, l.action, l.details, u.display_name, u.id as user_id
                FROM audit_logs l JOIN users u ON l.user_id = u.id
            WHERE 1=1
        """
        params = []
        
        if user_id:
            query += " AND l.user_id = %s"
            params.append(user_id)
        
        query += f" ORDER BY {sort_by} {sort_order} LIMIT %s"
        params.append(limit)
        
        cur.execute(query, tuple(params))
        res = cur.fetchall()
        return res
    finally:
        cur.close()
        conn.close()

def create_user_session(user_id, session_token, ip_address=None, user_agent=None, expires_in_days=30):
    """
    Создает новую сессию пользователя в базе данных
    - user_id: ID пользователя
    - session_token: Уникальный токен сессии (можно использовать Flask session ID)
    - ip_address: IP адрес пользователя
    - user_agent: User Agent браузера
    - expires_in_days: Количество дней до истечения сессии (по умолчанию 30)
    """
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        expires_at = datetime.now() + timedelta(days=expires_in_days)
        
        cur.execute("""
            INSERT INTO user_sessions (user_id, session_token, ip_address, user_agent, expires_at, is_active)
            VALUES (%s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (session_token) DO UPDATE SET
                is_active = TRUE,
                expires_at = EXCLUDED.expires_at,
                ip_address = EXCLUDED.ip_address,
                user_agent = EXCLUDED.user_agent
        """, (user_id, session_token, ip_address, user_agent, expires_at))
        
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Ошибка создания сессии: {e}")
    finally:
        cur.close()
        conn.close()

def deactivate_user_session(session_token):
    """
    Деактивирует сессию пользователя (при выходе)
    Устанавливает is_active = FALSE и expires_at на текущее время
    - session_token: Токен сессии для деактивации
    """
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        current_time = datetime.now()
        cur.execute("""
            UPDATE user_sessions
            SET is_active = FALSE, expires_at = %s
            WHERE session_token = %s
        """, (current_time, session_token))
        
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Ошибка деактивации сессии: {e}")
    finally:
        cur.close()
        conn.close()

def get_user_sessions(user_id, sort_by='created_at', sort_order='desc'):
    """
    Возвращает сессии пользователя с возможностью сортировки:
    - user_id: ID пользователя
    - sort_by: поле для сортировки ('created_at', 'expires_at', 'ip_address')
    - sort_order: 'asc' или 'desc'
    """
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # Валидация полей сортировки
        valid_sort_fields = ['created_at', 'expires_at', 'ip_address']
        if sort_by not in valid_sort_fields:
            sort_by = 'created_at'
        
        sort_order = 'DESC' if sort_order.lower() == 'desc' else 'ASC'
        
        query = f"""
            SELECT id, session_token, ip_address, user_agent, created_at, expires_at, is_active
            FROM user_sessions
            WHERE user_id = %s
            ORDER BY {sort_by} {sort_order}
        """
        
        cur.execute(query, (user_id,))
        res = cur.fetchall()
        return res
    finally:
        cur.close()
        conn.close()

def toggle_block_status(user_id):
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT is_blocked FROM users WHERE id=%s", (user_id,))
    curr = cur.fetchone()
    if not curr: return None
    new_s = not curr['is_blocked']
    cur.execute("UPDATE users SET is_blocked=%s WHERE id=%s", (new_s, user_id))
    conn.commit(); cur.close(); conn.close(); return new_s

def toggle_admin_role(user_id):
    """Переключает роль пользователя между 'admin' и 'user'"""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT role FROM users WHERE id=%s", (user_id,))
        curr = cur.fetchone()
        if not curr:
            return None
        new_role = 'admin' if curr['role'] != 'admin' else 'user'
        cur.execute("UPDATE users SET role=%s WHERE id=%s", (new_role, user_id))
        conn.commit()
        return new_role
    finally:
        cur.close()
        conn.close()

def get_user_preferences(user_id):
    """Получает настройки пользователя"""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM user_preferences WHERE user_id = %s", (user_id,))
        prefs = cur.fetchone()
        if not prefs:
            # Создаем настройки по умолчанию, если их нет
            cur.execute("""
                INSERT INTO user_preferences (user_id, language, notifications_enabled, 
                                             email_notifications, default_view, timezone)
                VALUES (%s, 'ru', TRUE, TRUE, 'dashboard', 'UTC')
                RETURNING *;
            """, (user_id,))
            prefs = cur.fetchone()
            conn.commit()
        return prefs
    finally:
        cur.close()
        conn.close()

def create_feedback(user_id, feedback_type, title, message, page_url=None, created_by_user_id=None):
    """Создает новую запись обратной связи"""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # Устанавливаем переменную сессии для триггеров
        if created_by_user_id:
            cur.execute("SET LOCAL app.current_user_id = %s", (str(created_by_user_id),))
        cur.execute("""
            INSERT INTO feedback (user_id, feedback_type, title, message, page_url)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *;
        """, (user_id, feedback_type, title, message, page_url))
        feedback = cur.fetchone()
        conn.commit()
        return feedback
    except Exception as e:
        conn.rollback()
        print(f"Ошибка создания feedback: {e}")
        return None
    finally:
        cur.close()
        conn.close()

def get_feedback_by_id(feedback_id):
    """Получает feedback по ID"""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT f.*, u.username, u.display_name, u.email
            FROM feedback f
            LEFT JOIN users u ON f.user_id = u.id
            WHERE f.id = %s
        """, (feedback_id,))
        return cur.fetchone()
    finally:
        cur.close()
        conn.close()

def get_all_feedback(status=None, feedback_type=None, limit=100, offset=0):
    """Получает все feedback с фильтрацией"""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        query = """
            SELECT f.*, u.username, u.display_name, u.email
            FROM feedback f
            LEFT JOIN users u ON f.user_id = u.id
            WHERE 1=1
        """
        params = []
        
        if status:
            query += " AND f.status = %s"
            params.append(status)
        
        if feedback_type:
            query += " AND f.feedback_type = %s"
            params.append(feedback_type)
        
        query += " ORDER BY f.created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        
        cur.execute(query, params)
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()

def update_feedback_status(feedback_id, status, admin_notes=None, priority=None, changed_by_user_id=None):
    """Обновляет статус feedback (для админов)"""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Устанавливаем переменную сессии для триггеров
        if changed_by_user_id:
            cur.execute("SET LOCAL app.current_user_id = %s", (str(changed_by_user_id),))
        
        updates = []
        params = []
        
        if status:
            updates.append("status = %s")
            params.append(status)
        
        if admin_notes is not None:
            updates.append("admin_notes = %s")
            params.append(admin_notes)
        
        if priority:
            updates.append("priority = %s")
            params.append(priority)
        
        if not updates:
            return False
        
        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(feedback_id)
        
        query = f"UPDATE feedback SET {', '.join(updates)} WHERE id = %s"
        cur.execute(query, params)
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"Ошибка обновления feedback: {e}")
        return False
    finally:
        cur.close()
        conn.close()

def get_feedback_count(status=None):
    """Получает количество feedback по статусу"""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        if status:
            cur.execute("SELECT COUNT(*) FROM feedback WHERE status = %s", (status,))
        else:
            cur.execute("SELECT COUNT(*) FROM feedback")
        return cur.fetchone()[0]
    finally:
        cur.close()
        conn.close()

def send_feedback_response_email(feedback, custom_message=None):
    """Отправляет ответ на feedback пользователю"""
    if not feedback.get('email'):
        return False, "У пользователя не указан email"
    
    user_email = feedback['email']
    feedback_type = feedback.get('feedback_type', 'other')
    feedback_title = feedback.get('title', 'Обратная связь')
    
    # Шаблоны сообщений для разных типов feedback
    templates = {
        'bug': {
            'subject': f'Ответ на ваше сообщение об ошибке: {feedback_title}',
            'greeting': 'Здравствуйте!',
            'intro': f'Спасибо за сообщение об ошибке "{feedback_title}". Мы внимательно изучили ваше обращение.',
            'default_message': '''Мы работаем над исправлением этой проблемы. Как только она будет решена, мы обязательно сообщим вам об этом.

Если у вас есть дополнительная информация, которая может помочь в решении проблемы, пожалуйста, ответьте на это письмо.''',
            'closing': 'С уважением,\nКоманда Spotify Analytics'
        },
        'feature_request': {
            'subject': f'Ответ на ваш запрос функции: {feedback_title}',
            'greeting': 'Здравствуйте!',
            'intro': f'Спасибо за ваш запрос функции "{feedback_title}". Мы ценим ваши предложения!',
            'default_message': '''Ваше предложение было рассмотрено нашей командой. Мы оцениваем его и учитываем при планировании новых функций.

Если ваша идея будет реализована, мы обязательно сообщим вам об этом в будущих обновлениях.''',
            'closing': 'С уважением,\nКоманда Spotify Analytics'
        },
        'complaint': {
            'subject': f'Ответ на вашу жалобу: {feedback_title}',
            'greeting': 'Здравствуйте!',
            'intro': f'Мы получили вашу жалобу "{feedback_title}" и очень сожалеем о возникших неудобствах.',
            'default_message': '''Мы серьезно относимся к каждой жалобе и обязательно разберемся в ситуации. Наша команда работает над решением проблемы.

Мы приносим извинения за доставленные неудобства и надеемся на ваше понимание.''',
            'closing': 'С уважением,\nКоманда Spotify Analytics'
        },
        'praise': {
            'subject': f'Спасибо за вашу благодарность!',
            'greeting': 'Здравствуйте!',
            'intro': f'Спасибо за вашу благодарность "{feedback_title}"!',
            'default_message': '''Ваши слова очень важны для нас и мотивируют нас продолжать улучшать сервис.

Мы рады, что вам нравится использовать Spotify Analytics, и будем продолжать работать над новыми функциями и улучшениями.''',
            'closing': 'С уважением и благодарностью,\nКоманда Spotify Analytics'
        },
        'other': {
            'subject': f'Ответ на ваше обращение: {feedback_title}',
            'greeting': 'Здравствуйте!',
            'intro': f'Спасибо за ваше обращение "{feedback_title}".',
            'default_message': '''Мы получили ваше сообщение и обязательно рассмотрим его. Если потребуется дополнительная информация, мы свяжемся с вами.''',
            'closing': 'С уважением,\nКоманда Spotify Analytics'
        }
    }
    
    template = templates.get(feedback_type, templates['other'])
    message_text = custom_message if custom_message else template['default_message']
    
    # Формируем текст письма
    email_body = f"""{template['greeting']}

{template['intro']}

{message_text}

{template['closing']}

---
Ваше оригинальное сообщение:
"{feedback.get('message', '')}"
"""
    
    try:
        msg = MIMEText(email_body, 'plain', 'utf-8')
        msg['Subject'] = template['subject']
        msg['From'] = f"{EMAIL_CONFIG['from_name']} <{EMAIL_CONFIG['from_email']}>"
        msg['To'] = user_email
        
        server = smtplib.SMTP(EMAIL_CONFIG['smtp_server'], EMAIL_CONFIG['smtp_port'], timeout=10)
        server.starttls()
        server.login(EMAIL_CONFIG['smtp_user'], EMAIL_CONFIG['smtp_password'])
        server.send_message(msg)
        server.quit()
        
        return True, "Сообщение успешно отправлено"
    except smtplib.SMTPConnectError as e:
        return False, f"Ошибка подключения к SMTP серверу: {str(e)}"
    except smtplib.SMTPAuthenticationError as e:
        return False, f"Ошибка аутентификации SMTP: {str(e)}"
    except smtplib.SMTPException as e:
        return False, f"Ошибка отправки email: {str(e)}"
    except Exception as e:
        return False, f"Неожиданная ошибка при отправке: {str(e)}"

def update_user_password(user_id, new_password, changed_by_user_id=None):
    """Обновляет пароль пользователя"""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Устанавливаем переменную сессии для триггеров
        if changed_by_user_id:
            cur.execute("SET LOCAL app.current_user_id = %s", (str(changed_by_user_id),))
        password_hash = generate_password_hash(new_password)
        cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user_id))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"Ошибка обновления пароля: {e}")
        return False
    finally:
        cur.close()
        conn.close()

def update_user_email(user_id, new_email, changed_by_user_id=None):
    """Обновляет email пользователя"""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Устанавливаем переменную сессии для триггеров
        if changed_by_user_id:
            cur.execute("SET LOCAL app.current_user_id = %s", (str(changed_by_user_id),))
        cur.execute("UPDATE users SET email = %s, email_verified = FALSE WHERE id = %s", 
                   (new_email, user_id))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"Ошибка обновления email: {e}")
        return False
    finally:
        cur.close()
        conn.close()

def update_user_display_name(user_id, new_display_name, changed_by_user_id=None):
    """Обновляет отображаемое имя пользователя"""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Устанавливаем переменную сессию для триггеров
        if changed_by_user_id:
            cur.execute("SET LOCAL app.current_user_id = %s", (str(changed_by_user_id),))
        cur.execute("UPDATE users SET display_name = %s WHERE id = %s", 
                   (new_display_name, user_id))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"Ошибка обновления имени: {e}")
        return False
    finally:
        cur.close()
        conn.close()

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_db_id' not in session: return redirect('/login-local')
        user = get_user_by_id(session['user_db_id'])
        if not user or user['role'] != 'admin': return "Доступ запрещен", 403
        return f(*args, **kwargs)
    return decorated_function

# ==========================================
# 3. ФУНКЦИИ SNOWFLAKE
# ==========================================

def save_tracks_to_snowflake_background(user_spotify_id, tracks_data, access_token=None):
    """Запускает сохранение треков в Snowflake в фоновом потоке"""
    def save_task():
        try:
            save_tracks_to_snowflake(user_spotify_id, tracks_data, access_token)
        except Exception as e:
            print(f"Background save error: {e}")
    
    thread = threading.Thread(target=save_task, daemon=True)
    thread.start()

def save_tracks_to_snowflake(user_spotify_id, tracks_data, access_token=None):
    """
    Сохраняет историю прослушиваний в Snowflake.
    Дополнительно, если есть access_token, пытается получить:
    - жанр артиста
    - изображение обложки трека
    """
    if not tracks_data or not user_spotify_id:
        log_snowflake("save_tracks_to_snowflake: нет данных или не указан user_spotify_id")
        return
    log_snowflake(f"save_tracks_to_snowflake: старт, пользователь={user_spotify_id}, треков={len(tracks_data)}")
    try:
        conn = snowflake.connector.connect(**SNOWFLAKE_CONFIG)
        cur = conn.cursor()
        cur.execute("USE SCHEMA PUBLIC")

        # Гарантируем наличие колонок в TRACKS
        try:
            cur.execute("ALTER TABLE IF EXISTS TRACKS ADD COLUMN IF NOT EXISTS GENRE STRING")
            cur.execute("ALTER TABLE IF EXISTS TRACKS ADD COLUMN IF NOT EXISTS IMAGE_URL STRING")
        except Exception as e:
            print(f"Warning: Could not add columns: {e}")

        artist_genre_cache = {}
        inserted_history = 0
        headers = None
        if access_token:
            headers = {'Authorization': f'Bearer {access_token}'}

        for item in tracks_data:
            t = item['track']
            track_id = t['id']
            p_at = item['played_at'].replace('Z', '')

            # Получаем изображение обложки
            image_url = None
            if t.get('album') and t['album'].get('images'):
                image_url = t['album']['images'][0].get('url') if t['album']['images'] else None

            # Пытаемся определить жанр по первому артисту
            genre = None
            if headers and t.get('artists'):
                artist_id = t['artists'][0].get('id')
                if artist_id:
                    if artist_id in artist_genre_cache:
                        genre = artist_genre_cache[artist_id]
                    else:
                        try:
                            ar_resp = requests.get(API_BASE_URL + f'artists/{artist_id}', headers=headers)
                            if ar_resp.status_code == 200:
                                genres = ar_resp.json().get('genres') or []
                                genre = genres[0] if genres else None
                            artist_genre_cache[artist_id] = genre
                        except Exception:
                            artist_genre_cache[artist_id] = None

            # Upsert Track (с жанром и изображением)
            cur.execute("""
                MERGE INTO TRACKS t
                USING (SELECT %s AS ID,
                              %s AS NAME,
                              %s AS ARTIST,
                              %s AS DUR,
                              %s AS POP,
                              %s AS GENRE,
                              %s AS IMG) s
                ON t.TRACK_ID = s.ID
                WHEN NOT MATCHED THEN
                    INSERT (TRACK_ID, NAME, ARTIST, DURATION_MS, POPULARITY, GENRE, IMAGE_URL)
                    VALUES (s.ID, s.NAME, s.ARTIST, s.DUR, s.POP, s.GENRE, s.IMG)
                WHEN MATCHED THEN
                    UPDATE SET
                        NAME = s.NAME,
                        ARTIST = s.ARTIST,
                        DURATION_MS = s.DUR,
                        POPULARITY = s.POP,
                        GENRE = COALESCE(s.GENRE, t.GENRE),
                        IMAGE_URL = COALESCE(s.IMG, t.IMAGE_URL)
            """, (
                track_id,
                t['name'],
                t['artists'][0]['name'] if t.get('artists') else None,
                t['duration_ms'],
                t.get('popularity'),
                genre,
                image_url
            ))

            # Insert History
            cur.execute(
                "SELECT COUNT(*) FROM LISTENING_HISTORY WHERE USER_SPOTIFY_ID=%s AND TRACK_ID=%s AND PLAYED_AT=%s",
                (user_spotify_id, track_id, p_at)
            )
            if cur.fetchone()[0] == 0:
                cur.execute(
                    "INSERT INTO LISTENING_HISTORY (USER_SPOTIFY_ID, TRACK_ID, PLAYED_AT) VALUES (%s, %s, %s)",
                    (user_spotify_id, track_id, p_at)
                )
                inserted_history += 1

        conn.commit()
        log_snowflake(
            f"save_tracks_to_snowflake: успешно. обновлено/вставлено треков={len(tracks_data)}, "
            f"новых записей в LISTENING_HISTORY={inserted_history}"
        )
    except Exception as e:
        log_snowflake(f"save_tracks_to_snowflake: ОШИБКА {e}")
        import traceback
        traceback.print_exc()
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()

def get_recommended_artists_from_snowflake(user_spotify_id, limit=5):
    """Получает рекомендации артистов на основе предпочтений пользователя"""
    if not user_spotify_id:
        log_snowflake("get_recommended_artists_from_snowflake: не указан user_spotify_id")
        return []
    log_snowflake(
        f"get_recommended_artists_from_snowflake: старт, user_spotify_id={user_spotify_id}, limit={limit}"
    )
    try:
        conn = snowflake.connector.connect(**SNOWFLAKE_CONFIG)
        cur = conn.cursor()
        cur.execute("USE SCHEMA PUBLIC")
        
        # Проверяем, есть ли таблица ARTISTS
        try:
            cur.execute("SELECT COUNT(*) FROM ARTISTS")
        except:
            # Если таблицы нет, возвращаем пустой список
            return []
        
        # Получаем топ-3 жанра пользователя
        cur.execute("""
            SELECT t.GENRE
            FROM LISTENING_HISTORY lh
            JOIN TRACKS t ON lh.TRACK_ID = t.TRACK_ID
            WHERE lh.USER_SPOTIFY_ID = %s
                AND t.GENRE IS NOT NULL
                AND t.GENRE != 'Не определен'
            GROUP BY t.GENRE
            ORDER BY COUNT(*) DESC
            LIMIT 3
        """, (user_spotify_id,))
        top_genres = [row[0] for row in cur.fetchall()]
        
        # Получаем артистов, которых пользователь уже слушал
        cur.execute("""
            SELECT DISTINCT t.ARTIST
            FROM LISTENING_HISTORY lh
            JOIN TRACKS t ON lh.TRACK_ID = t.TRACK_ID
            WHERE lh.USER_SPOTIFY_ID = %s
                AND t.ARTIST IS NOT NULL
        """, (user_spotify_id,))
        listened_artists = {row[0].lower() for row in cur.fetchall()}
        
        # Инициализируем список рекомендованных артистов
        recommended = []
        
        if top_genres:
            # Ищем артистов в тех же жанрах, которых пользователь еще не слушал
            # В Snowflake ARRAY содержит жанры, нужно проверить пересечение
            
            # Получаем всех артистов с жанрами (увеличиваем лимит для лучшего выбора)
            cur.execute("""
                SELECT a.ARTIST_ID, a.NAME, a.GENRES, a.POPULARITY, a.FOLLOWERS, a.IMAGE_URL
                FROM ARTISTS a
                WHERE a.GENRES IS NOT NULL
                AND a.POPULARITY IS NOT NULL
                AND a.NAME IS NOT NULL
                ORDER BY a.POPULARITY DESC
                LIMIT 1000
            """)
            
            all_artists = cur.fetchall()

            # Считаем «оценку соответствия» артиста вкусам пользователя:
            # чем больше пересечение жанров + разумная популярность, тем выше.
            scored_candidates = []
            
            for artist_row in all_artists:
                artist_id, name, genres, popularity, followers, image_url = artist_row
                
                if not genres or not name or name.lower() in listened_artists:
                    continue

                # genres - это ARRAY в Snowflake, конвертируем в список нижнего регистра
                if isinstance(genres, list):
                    artist_genres = [str(g).lower() if g else "" for g in genres]
                elif genres:
                    try:
                        artist_genres = [str(g).lower() for g in genres] if hasattr(genres, "__iter__") else [str(genres).lower()]
                    except Exception:
                        artist_genres = []
                else:
                    artist_genres = []
                    
                if not artist_genres:
                    continue

                # Считаем пересечение с топ‑жанрами пользователя
                overlap = [g for g in artist_genres if g in [tg.lower() for tg in top_genres]]
                if not overlap:
                    continue

                # Оценка: количество совпавших жанров + нормализованная популярность
                pop = popularity or 0
                score = len(overlap) * 100 + pop

                # Преобразуем genres в список для шаблона, фильтруя пустые значения
                # genres уже обработан выше как artist_genres, но нужно для шаблона
                genres_list = []
                if isinstance(genres, list):
                    genres_list = [str(g).strip() for g in genres if g and str(g).strip() and str(g).strip() != 'None' and str(g).strip() != 'null' and str(g).strip() != '']
                elif isinstance(genres, str):
                    # Если строка, пытаемся распарсить как JSON массив
                    try:
                        import json
                        if genres.strip().startswith('[') and genres.strip().endswith(']'):
                            parsed = json.loads(genres)
                            if isinstance(parsed, list):
                                genres_list = [str(g).strip() for g in parsed if g and str(g).strip() and str(g).strip() != 'None' and str(g).strip() != 'null' and str(g).strip() != '']
                        else:
                            genres_list = [g.strip().strip('"').strip("'") for g in genres.split(',') if g.strip()]
                    except:
                        genres_list = [g.strip().strip('"').strip("'") for g in genres.split(',') if g.strip()]
                elif genres and hasattr(genres, "__iter__") and not isinstance(genres, (str, bytes)):
                    try:
                        genres_list = [str(g).strip() for g in genres if g and str(g).strip() and str(g).strip() != 'None' and str(g).strip() != 'null' and str(g).strip() != '']
                    except:
                        genres_list = []
                # Убираем дубликаты и пустые значения
                genres_list = list(dict.fromkeys([g for g in genres_list if g]))

                scored_candidates.append(
                    (
                        score,
                        {
                            "artist_id": artist_id,
                            "name": name,
                            "genres": genres_list,
                            "popularity": popularity,
                            "followers": followers,
                            "image_url": image_url,
                        },
                    )
                )

            # Сортируем по убыванию оценки и берем top-N
            scored_candidates.sort(key=lambda x: x[0], reverse=True)
            for _, artist_data in scored_candidates[:limit]:
                recommended.append(artist_data)
        else:
            # Если нет жанров, рекомендуем популярных артистов, которых пользователь не слушал
            cur.execute("""
                SELECT a.ARTIST_ID, a.NAME, a.GENRES, a.POPULARITY, a.FOLLOWERS, a.IMAGE_URL
                FROM ARTISTS a
                WHERE a.POPULARITY IS NOT NULL
                AND a.NAME IS NOT NULL
                ORDER BY a.POPULARITY DESC
                LIMIT %s
            """, (limit * 3,))
            
            all_artists = cur.fetchall()
            for artist_row in all_artists:
                if len(recommended) >= limit:
                    break
                artist_id, name, genres, popularity, followers, image_url = artist_row
                if name and name.lower() not in listened_artists:
                    # Преобразуем genres в список для шаблона, фильтруя пустые значения
                    genres_list = []
                    if isinstance(genres, list):
                        genres_list = [str(g).strip() for g in genres if g and str(g).strip() and str(g).strip() != 'None' and str(g).strip() != 'null']
                    elif genres and hasattr(genres, '__iter__'):
                        try:
                            genres_list = [str(g).strip() for g in genres if g and str(g).strip() and str(g).strip() != 'None' and str(g).strip() != 'null']
                        except:
                            genres_list = []
                    # Убираем дубликаты и пустые значения
                    genres_list = list(dict.fromkeys([g for g in genres_list if g]))
                    recommended.append({
                        'artist_id': artist_id,
                        'name': name,
                        'genres': genres_list,
                        'popularity': popularity,
                        'followers': followers,
                        'image_url': image_url
                    })
        
        # Если получили меньше нужного количества, дополняем популярными артистами
        if len(recommended) < limit:
            needed = limit - len(recommended)
            recommended_ids = {a.get('artist_id') for a in recommended if a.get('artist_id')}
            recommended_names = {a.get('name', '').lower() for a in recommended if a.get('name')}
            
            # Получаем дополнительные популярных артистов
            try:
                if recommended_ids and recommended_names:
                    # Исключаем и по ID, и по имени
                    placeholders_ids = ','.join(['%s'] * len(recommended_ids))
                    placeholders_names = ','.join(['%s'] * len(recommended_names))
                    cur.execute(f"""
                        SELECT a.ARTIST_ID, a.NAME, a.GENRES, a.POPULARITY, a.FOLLOWERS, a.IMAGE_URL
                        FROM ARTISTS a
                        WHERE a.POPULARITY IS NOT NULL
                        AND a.NAME IS NOT NULL
                        AND a.ARTIST_ID NOT IN ({placeholders_ids})
                        AND LOWER(a.NAME) NOT IN ({placeholders_names})
                        ORDER BY a.POPULARITY DESC
                        LIMIT %s
                    """, tuple(recommended_ids) + tuple(recommended_names) + (needed * 3,))
                elif recommended_names:
                    # Исключаем только по имени
                    placeholders_names = ','.join(['%s'] * len(recommended_names))
                    cur.execute(f"""
                        SELECT a.ARTIST_ID, a.NAME, a.GENRES, a.POPULARITY, a.FOLLOWERS, a.IMAGE_URL
                        FROM ARTISTS a
                        WHERE a.POPULARITY IS NOT NULL
                        AND a.NAME IS NOT NULL
                        AND LOWER(a.NAME) NOT IN ({placeholders_names})
                        ORDER BY a.POPULARITY DESC
                        LIMIT %s
                    """, tuple(recommended_names) + (needed * 3,))
                else:
                    # Просто берем популярных артистов
                    cur.execute("""
                        SELECT a.ARTIST_ID, a.NAME, a.GENRES, a.POPULARITY, a.FOLLOWERS, a.IMAGE_URL
                        FROM ARTISTS a
                        WHERE a.POPULARITY IS NOT NULL
                        AND a.NAME IS NOT NULL
                        ORDER BY a.POPULARITY DESC
                        LIMIT %s
                    """, (needed * 3,))
                
                additional_artists = cur.fetchall()
                for artist_row in additional_artists:
                    if len(recommended) >= limit:
                        break
                    artist_id, name, genres, popularity, followers, image_url = artist_row
                    if name and name.lower() not in recommended_names:
                        # Преобразуем genres в список для шаблона, фильтруя пустые значения
                        genres_list = []
                        if isinstance(genres, list):
                            genres_list = [str(g).strip() for g in genres if g and str(g).strip() and str(g).strip() != 'None' and str(g).strip() != 'null']
                        elif genres and hasattr(genres, '__iter__'):
                            try:
                                genres_list = [str(g).strip() for g in genres if g and str(g).strip() and str(g).strip() != 'None' and str(g).strip() != 'null']
                            except:
                                genres_list = []
                        # Убираем дубликаты и пустые значения
                        genres_list = list(dict.fromkeys([g for g in genres_list if g]))
                        recommended.append({
                            'artist_id': artist_id,
                            'name': name,
                            'genres': genres_list,
                            'popularity': popularity,
                            'followers': followers,
                            'image_url': image_url
                        })
            except Exception as e:
                log_snowflake(f"get_recommended_artists_from_snowflake: ошибка при дополнении списка артистов: {e}")
                import traceback
                traceback.print_exc()
        
        # Если все еще недостаточно, просто берем любых популярных артистов (даже если они уже в списке)
        if len(recommended) < limit:
            try:
                needed = limit - len(recommended)
                cur.execute("""
                    SELECT a.ARTIST_ID, a.NAME, a.GENRES, a.POPULARITY, a.FOLLOWERS, a.IMAGE_URL
                    FROM ARTISTS a
                    WHERE a.POPULARITY IS NOT NULL
                    AND a.NAME IS NOT NULL
                    ORDER BY a.POPULARITY DESC
                    LIMIT %s
                """, (needed * 2,))
                
                all_artists = cur.fetchall()
                for artist_row in all_artists:
                    if len(recommended) >= limit:
                        break
                    artist_id, name, genres, popularity, followers, image_url = artist_row
                    # Проверяем, нет ли уже такого артиста
                    if name and not any(a.get('artist_id') == artist_id or a.get('name', '').lower() == name.lower() for a in recommended):
                        genres_list = []
                        if isinstance(genres, list):
                            genres_list = [str(g).strip() for g in genres if g and str(g).strip() and str(g).strip() != 'None' and str(g).strip() != 'null']
                        elif genres and hasattr(genres, '__iter__'):
                            try:
                                genres_list = [str(g).strip() for g in genres if g and str(g).strip() and str(g).strip() != 'None' and str(g).strip() != 'null']
                            except:
                                genres_list = []
                        # Убираем дубликаты и пустые значения
                        genres_list = list(dict.fromkeys([g for g in genres_list if g]))
                        recommended.append({
                            'artist_id': artist_id,
                            'name': name,
                            'genres': genres_list,
                            'popularity': popularity,
                            'followers': followers,
                            'image_url': image_url
                        })
            except Exception as e:
                log_snowflake(f"get_recommended_artists_from_snowflake: ошибка при финальном дополнении списка артистов: {e}")
        
        # Возвращаем минимум limit артистов
        final_artists = recommended[:limit]
        log_snowflake(
            f"get_recommended_artists_from_snowflake: завершено, отобрано рекомендаций={len(final_artists)}"
        )
        return final_artists
    except Exception as e:
        log_snowflake(f"get_recommended_artists_from_snowflake: ОШИБКА {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()


def get_genre_distribution_from_snowflake(user_spotify_id, limit=5):
    """Возвращает топ жанров по количеству прослушиваний (для doughnut chart).
    Исключает жанр 'Не определен' и NULL значения."""
    if not user_spotify_id:
        log_snowflake("get_genre_distribution_from_snowflake: не указан user_spotify_id")
        return []
    log_snowflake(
        f"get_genre_distribution_from_snowflake: старт, user_spotify_id={user_spotify_id}, limit={limit}"
    )
    try:
        conn = snowflake.connector.connect(**SNOWFLAKE_CONFIG)
        cur = conn.cursor()
        cur.execute("USE SCHEMA PUBLIC")
        cur.execute("""
            SELECT t.GENRE AS genre, COUNT(*) AS play_count
            FROM LISTENING_HISTORY lh
            JOIN TRACKS t ON lh.TRACK_ID = t.TRACK_ID
            WHERE lh.USER_SPOTIFY_ID = %s
                AND t.GENRE IS NOT NULL
                AND t.GENRE != 'Не определен'
            GROUP BY genre
            ORDER BY play_count DESC
            LIMIT %s
        """, (user_spotify_id, limit))
        results = cur.fetchall()
        data = [{'genre': row[0], 'count': int(row[1])} for row in results]
        log_snowflake(
            f"get_genre_distribution_from_snowflake: найдено жанров={len(data)}"
        )
        return data
    except Exception as e:
        log_snowflake(f"get_genre_distribution_from_snowflake: ОШИБКА {e}")
        return []
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()


def get_total_minutes_listened_from_snowflake(user_spotify_id):
    """Возвращает общее количество минут прослушивания для пользователя."""
    if not user_spotify_id:
        log_snowflake("get_total_minutes_listened_from_snowflake: не указан user_spotify_id")
        return 0
    log_snowflake(
        f"get_total_minutes_listened_from_snowflake: старт, user_spotify_id={user_spotify_id}"
    )
    try:
        conn = snowflake.connector.connect(**SNOWFLAKE_CONFIG)
        cur = conn.cursor()
        cur.execute("USE SCHEMA PUBLIC")
        # Суммируем длительность треков (в мс) по всем прослушиваниям пользователя
        cur.execute(
            """
            SELECT SUM(t.DURATION_MS) / 60000.0 AS minutes
            FROM LISTENING_HISTORY lh
            JOIN TRACKS t ON lh.TRACK_ID = t.TRACK_ID
            WHERE lh.USER_SPOTIFY_ID = %s
        """,
            (user_spotify_id,),
        )
        row = cur.fetchone()
        minutes = int(row[0]) if row and row[0] is not None else 0
        log_snowflake(
            f"get_total_minutes_listened_from_snowflake: minutes={minutes}"
        )
        return minutes
    except Exception as e:
        log_snowflake(
            f"get_total_minutes_listened_from_snowflake: ОШИБКА {e}"
        )
        import traceback

        traceback.print_exc()
        return 0
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()


def generate_user_vibe(user_spotify_id):
    """Генерирует креативный 'вайб' пользователя на основе его музыкальных предпочтений"""
    if not user_spotify_id:
        return "Музыкальный исследователь"
    
    # Получаем топ жанры пользователя
    genre_dist = get_genre_distribution_from_snowflake(user_spotify_id, limit=5)
    
    if not genre_dist:
        return "Музыкальный исследователь"
    
    # Нормализуем названия жанров (приводим к нижнему регистру для сравнения)
    genres = [g['genre'].lower() for g in genre_dist]
    top_genre = genres[0] if genres else None
    
    # Маппинг жанров на вайбы
    vibe_map = {
        # Поп
        'pop': 'Флексящий поп-исполнитель',
        'pop-punk': 'Флексящий поп-панк',
        'pop rock': 'Флексящий поп-рок',
        'dance pop': 'Танцующий поп-энтузиаст',
        'indie pop': 'Инди-поп эстет',
        
        # Рок
        'rock': 'Классический рокер',
        'alternative rock': 'Альтернативный рокер',
        'indie rock': 'Инди-рок меломан',
        'hard rock': 'Хард-рок бунтарь',
        'punk': 'Панк-революционер',
        'metal': 'Метал-воин',
        
        # Хип-хоп
        'hip hop': 'Хип-хоп ценитель',
        'rap': 'Рэп-энтузиаст',
        'trap': 'Трэп-фанат',
        'r&b': 'R&B ценитель',
        
        # Электронная музыка
        'electronic': 'Электронный меломан',
        'edm': 'EDM фанат',
        'house': 'Хаус-энтузиаст',
        'techno': 'Техно-любитель',
        'dubstep': 'Дабстеп-фанат',
        
        # Другие
        'jazz': 'Джазовый ценитель',
        'blues': 'Блюзовый меломан',
        'country': 'Кантри-энтузиаст',
        'folk': 'Фолк-любитель',
        'reggae': 'Регги-фанат',
        'latin': 'Латино-энтузиаст',
        'classical': 'Классический меломан',
        'acoustic': 'Акустический ценитель',
        'soul': 'Соул-любитель',
        'funk': 'Фанк-энтузиаст',
    }
    
    # Комбинированные вайбы для нескольких жанров
    if len(genres) >= 2:
        genre_combo = '+'.join(sorted(genres[:2]))
        
        combo_vibes = {
            'pop+rock': 'Поп-рок гибрид',
            'pop+hip hop': 'Поп-хип-хоп миксер',
            'pop+electronic': 'Поп-электронный фанат',
            'rock+metal': 'Рок-метал бунтарь',
            'hip hop+rap': 'Хип-хоп ценитель',
            'electronic+edm': 'Электронный энтузиаст',
            'indie+pop': 'Инди-поп эстет',
            'indie+rock': 'Инди-рок меломан',
            'acoustic+folk': 'Акустический фолк-любитель',
            'jazz+blues': 'Джаз-блюз ценитель',
        }
        
        if genre_combo in combo_vibes:
            return combo_vibes[genre_combo]
    
    # Одиночные жанры
    for genre_key, vibe in vibe_map.items():
        if genre_key in top_genre or top_genre in genre_key:
            return vibe
    
    # Если жанр не найден в маппинге, создаем креативный вайб на основе названия
    if top_genre:
        # Берем первое слово из жанра и добавляем креативное описание
        genre_words = top_genre.split()
        if genre_words:
            base_word = genre_words[0].capitalize()
            return f'{base_word}-энтузиаст'
    
    return "Музыкальный исследователь"

def get_top_tracks_from_snowflake(user_spotify_id, limit=5):
    """Возвращает топ треков по количеству прослушиваний из Snowflake с изображениями"""
    if not user_spotify_id:
        log_snowflake("get_top_tracks_from_snowflake: не указан user_spotify_id")
        return []
    log_snowflake(
        f"get_top_tracks_from_snowflake: старт, user_spotify_id={user_spotify_id}, limit={limit}"
    )
    try:
        conn = snowflake.connector.connect(**SNOWFLAKE_CONFIG)
        cur = conn.cursor()
        cur.execute("USE SCHEMA PUBLIC")
        cur.execute("""
            SELECT t.TRACK_ID, t.NAME, t.ARTIST, t.IMAGE_URL, COUNT(*) as play_count
            FROM LISTENING_HISTORY lh
            JOIN TRACKS t ON lh.TRACK_ID = t.TRACK_ID
            WHERE lh.USER_SPOTIFY_ID = %s
            GROUP BY t.TRACK_ID, t.NAME, t.ARTIST, t.IMAGE_URL
            ORDER BY play_count DESC
            LIMIT %s
        """, (user_spotify_id, limit))
        results = cur.fetchall()
        data = [{'track_id': row[0], 'name': row[1], 'artist': row[2], 'image_url': row[3], 'count': int(row[4])} for row in results]
        log_snowflake(
            f"get_top_tracks_from_snowflake: найдено треков={len(data)}"
        )
        return data
    except Exception as e:
        log_snowflake(f"Snowflake Error (get_top_tracks): ОШИБКА {e}")
        return []
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()

def get_track_full_info_from_snowflake(track_id):
    """Получает полную информацию о треке из Snowflake"""
    if not track_id:
        log_snowflake("get_track_full_info_from_snowflake: не указан track_id")
        return None
    log_snowflake(f"get_track_full_info_from_snowflake: старт, track_id={track_id}")
    try:
        conn = snowflake.connector.connect(**SNOWFLAKE_CONFIG)
        cur = conn.cursor()
        cur.execute("USE SCHEMA PUBLIC")
        cur.execute("""
            SELECT TRACK_ID, NAME, ARTIST, DURATION_MS, POPULARITY, GENRE, IMAGE_URL
            FROM TRACKS
            WHERE TRACK_ID = %s
        """, (track_id,))
        result = cur.fetchone()
        if result:
            info = {
                'track_id': result[0],
                'name': result[1],
                'artist': result[2],
                'duration_ms': result[3],
                'popularity': result[4],
                'genre': result[5],
                'image_url': result[6]
            }
            log_snowflake("get_track_full_info_from_snowflake: трек найден")
            return info
        log_snowflake("get_track_full_info_from_snowflake: трек не найден")
        return None
    except Exception as e:
        log_snowflake(f"Snowflake Error (get_track_full_info): ОШИБКА {e}")
        return None
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()

def get_recommendations_from_snowflake(user_spotify_id, limit=10, min_limit=10):
    """Получает рекомендации треков на основе предпочтений пользователя
    
    Требования к данным:
    - Минимум: хотя бы 1 прослушанный трек (для базовых рекомендаций)
    - Для персонализированных рекомендаций: минимум 3-5 треков с определенными жанрами
    - Оптимально: 10+ треков с различными жанрами для более точных рекомендаций
    """
    if not user_spotify_id:
        log_snowflake("get_recommendations_from_snowflake: не указан user_spotify_id")
        return []
    log_snowflake(
        f"get_recommendations_from_snowflake: старт, user_spotify_id={user_spotify_id}, "
        f"limit={limit}, min_limit={min_limit}"
    )
    try:
        conn = snowflake.connector.connect(**SNOWFLAKE_CONFIG)
        cur = conn.cursor()
        cur.execute("USE SCHEMA PUBLIC")
        
        # Проверяем, сколько треков прослушал пользователь
        cur.execute("""
            SELECT COUNT(DISTINCT TRACK_ID) 
            FROM LISTENING_HISTORY 
            WHERE USER_SPOTIFY_ID = %s
        """, (user_spotify_id,))
        tracks_count = cur.fetchone()[0]
        
        # Если пользователь прослушал менее 3 треков, возвращаем популярные треки
        if tracks_count < 3:
            log_snowflake(
                f"get_recommendations_from_snowflake: у пользователя мало истории (tracks_count={tracks_count}), "
                "используем глобально популярные треки"
            )
            cur.execute("""
                SELECT DISTINCT t.TRACK_ID, t.NAME, t.ARTIST, t.IMAGE_URL, t.POPULARITY, t.GENRE
                FROM TRACKS t
                WHERE t.TRACK_ID NOT IN (
                    SELECT DISTINCT TRACK_ID FROM LISTENING_HISTORY WHERE USER_SPOTIFY_ID = %s
                )
                AND t.POPULARITY IS NOT NULL
                ORDER BY t.POPULARITY DESC
                LIMIT %s
            """, (user_spotify_id, limit))
        else:
            # Получаем топ-3 жанра пользователя
            log_snowflake(
                f"get_recommendations_from_snowflake: tracks_count={tracks_count}, строим рекомендации по жанрам/артистам"
            )
            cur.execute("""
                SELECT t.GENRE
                FROM LISTENING_HISTORY lh
                JOIN TRACKS t ON lh.TRACK_ID = t.TRACK_ID
                WHERE lh.USER_SPOTIFY_ID = %s
                    AND t.GENRE IS NOT NULL
                    AND t.GENRE != 'Не определен'
                GROUP BY t.GENRE
                ORDER BY COUNT(*) DESC
                LIMIT 3
            """, (user_spotify_id,))
            top_genres = [row[0] for row in cur.fetchall()]
            log_snowflake(f"get_recommendations_from_snowflake: top_genres={top_genres}")
            
            if not top_genres:
                cur.execute("""
                    SELECT t.ARTIST
                    FROM LISTENING_HISTORY lh
                    JOIN TRACKS t ON lh.TRACK_ID = t.TRACK_ID
                    WHERE lh.USER_SPOTIFY_ID = %s
                        AND t.ARTIST IS NOT NULL
                    GROUP BY t.ARTIST
                    ORDER BY COUNT(*) DESC
                    LIMIT 3
                """, (user_spotify_id,))
                top_artists = [row[0] for row in cur.fetchall()]
                log_snowflake(f"get_recommendations_from_snowflake: top_artists={top_artists}")
                
                if top_artists:
                    placeholders = ','.join(['%s'] * len(top_artists))
                    cur.execute(f"""
                        SELECT DISTINCT t.TRACK_ID, t.NAME, t.ARTIST, t.IMAGE_URL, t.POPULARITY, t.GENRE
                        FROM TRACKS t
                        WHERE t.ARTIST IN ({placeholders})
                        AND t.TRACK_ID NOT IN (
                            SELECT DISTINCT TRACK_ID FROM LISTENING_HISTORY WHERE USER_SPOTIFY_ID = %s
                        )
                        AND t.POPULARITY IS NOT NULL
                        ORDER BY t.POPULARITY DESC
                        LIMIT %s
                    """, tuple(top_artists) + (user_spotify_id, limit))
                else:
                    cur.execute("""
                        SELECT DISTINCT t.TRACK_ID, t.NAME, t.ARTIST, t.IMAGE_URL, t.POPULARITY, t.GENRE
                        FROM TRACKS t
                        WHERE t.TRACK_ID NOT IN (
                            SELECT DISTINCT TRACK_ID FROM LISTENING_HISTORY WHERE USER_SPOTIFY_ID = %s
                        )
                        AND t.POPULARITY IS NOT NULL
                        ORDER BY t.POPULARITY DESC
                        LIMIT %s
                    """, (user_spotify_id, limit))
            else:
                placeholders = ','.join(['%s'] * len(top_genres))
                cur.execute(f"""
                    SELECT DISTINCT t.TRACK_ID, t.NAME, t.ARTIST, t.IMAGE_URL, t.POPULARITY, t.GENRE
                    FROM TRACKS t
                    WHERE t.GENRE IN ({placeholders})
                    AND t.TRACK_ID NOT IN (
                        SELECT DISTINCT TRACK_ID FROM LISTENING_HISTORY WHERE USER_SPOTIFY_ID = %s
                    )
                    AND t.POPULARITY IS NOT NULL
                    ORDER BY t.POPULARITY DESC
                    LIMIT %s
                """, tuple(top_genres) + (user_spotify_id, limit))
        
        results = cur.fetchall()
        tracks = [{
            'track_id': row[0],
            'name': row[1],
            'artist': row[2],
            'image_url': row[3],
            'popularity': row[4],
            'genre': row[5]
        } for row in results]
        
        if len(tracks) < min_limit:
            # Получаем уже рекомендованные track_id и прослушанные треки
            recommended_ids = {t['track_id'] for t in tracks}
            needed = min_limit - len(tracks)
            
            if recommended_ids:
                placeholders = ','.join(['%s'] * len(recommended_ids))
                cur.execute(f"""
                    SELECT DISTINCT t.TRACK_ID, t.NAME, t.ARTIST, t.IMAGE_URL, t.POPULARITY, t.GENRE
                    FROM TRACKS t
                    WHERE t.TRACK_ID NOT IN (
                        SELECT DISTINCT TRACK_ID FROM LISTENING_HISTORY WHERE USER_SPOTIFY_ID = %s
                    )
                    AND t.TRACK_ID NOT IN ({placeholders})
                    AND t.POPULARITY IS NOT NULL
                    ORDER BY t.POPULARITY DESC
                    LIMIT %s
                """, (user_spotify_id,) + tuple(recommended_ids) + (needed,))
            else:
                cur.execute("""
                    SELECT DISTINCT t.TRACK_ID, t.NAME, t.ARTIST, t.IMAGE_URL, t.POPULARITY, t.GENRE
                    FROM TRACKS t
                    WHERE t.TRACK_ID NOT IN (
                        SELECT DISTINCT TRACK_ID FROM LISTENING_HISTORY WHERE USER_SPOTIFY_ID = %s
                    )
                    AND t.POPULARITY IS NOT NULL
                    ORDER BY t.POPULARITY DESC
                    LIMIT %s
                """, (user_spotify_id, needed))
            
            additional_results = cur.fetchall()
            tracks.extend([{
                'track_id': row[0],
                'name': row[1],
                'artist': row[2],
                'image_url': row[3],
                'popularity': row[4],
                'genre': row[5]
            } for row in additional_results])
        
        final_tracks = tracks[:max(limit, min_limit)]
        log_snowflake(
            f"get_recommendations_from_snowflake: завершено, tracks_count={tracks_count}, "
            f"отобрано рекомендаций={len(final_tracks)}"
        )
        return final_tracks
    except Exception as e:
        log_snowflake(f"get_recommendations_from_snowflake: ОШИБКА {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()


def get_listening_activity_by_hour_from_snowflake(user_spotify_id):
    """Получает активность прослушиваний по часам дня из Snowflake"""
    if not user_spotify_id: return []
    try:
        conn = snowflake.connector.connect(**SNOWFLAKE_CONFIG)
        cur = conn.cursor()
        cur.execute("USE SCHEMA PUBLIC")
        cur.execute("""
            SELECT EXTRACT(HOUR FROM PLAYED_AT::TIMESTAMP_NTZ) as hour,
                   COUNT(*) as play_count
            FROM LISTENING_HISTORY
            WHERE USER_SPOTIFY_ID = %s
            GROUP BY hour
            ORDER BY hour
        """, (user_spotify_id,))
        results = cur.fetchall()
        print(f"DEBUG: hour results = {results}")
        hour_data = {int(row[0]): int(row[1]) for row in results if row[0] is not None}
        result = [{'hour': hour, 'count': hour_data.get(hour, 0)} for hour in range(24)]
        print(f"DEBUG: hour result length = {len(result)}, has data = {any(item['count'] > 0 for item in result)}")
        if not any(item['count'] > 0 for item in result):
            return []
        return result
    except Exception as e:
        print(f"Snowflake Error (get_listening_activity_by_hour): {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        if 'cur' in locals(): cur.close()
        if 'conn' in locals(): conn.close()

def get_top_artists_from_snowflake(user_spotify_id, limit=10, access_token=None):
    """Возвращает топ артистов по количеству прослушиваний с изображениями"""
    if not user_spotify_id:
        log_snowflake("get_top_artists_from_snowflake: не указан user_spotify_id")
        return []
    log_snowflake(f"get_top_artists_from_snowflake: старт, user_spotify_id={user_spotify_id}, limit={limit}")
    try:
        conn = snowflake.connector.connect(**SNOWFLAKE_CONFIG)
        cur = conn.cursor()
        cur.execute("USE SCHEMA PUBLIC")
        # Получаем топ артистов с изображениями:
        # 1. Сначала пытаемся из ARTISTS
        # 2. Если нет, пытаемся из TRACKS (берем первое доступное изображение трека артиста)
        # 3. Используем COALESCE для выбора первого не-NULL значения
        cur.execute("""
            SELECT 
                t.ARTIST, 
                COUNT(*) as play_count, 
                COALESCE(
                    MAX(a.IMAGE_URL),
                    MAX(t.IMAGE_URL)
                ) as image_url,
                MAX(t.ARTIST_ID) as artist_id
            FROM LISTENING_HISTORY lh
            JOIN TRACKS t ON lh.TRACK_ID = t.TRACK_ID
            LEFT JOIN ARTISTS a ON t.ARTIST_ID = a.ARTIST_ID
            WHERE lh.USER_SPOTIFY_ID = %s
                AND t.ARTIST IS NOT NULL
            GROUP BY t.ARTIST
            ORDER BY play_count DESC
            LIMIT %s
        """, (user_spotify_id, limit))
        results = cur.fetchall()
        data = []
        for row in results:
            artist_name = row[0]
            count = int(row[1])
            image_url = row[2]
            artist_id = row[3]
            
            # Если изображения все еще нет и есть access_token, пытаемся получить через Spotify API
            if not image_url and artist_id and access_token:
                try:
                    headers = {'Authorization': f'Bearer {access_token}'}
                    artist_resp = requests.get(API_BASE_URL + f'artists/{artist_id}', headers=headers, timeout=3)
                    if artist_resp.status_code == 200:
                        spotify_data = artist_resp.json()
                        if spotify_data.get('images') and len(spotify_data['images']) > 0:
                            image_url = spotify_data['images'][0].get('url')
                            # Обновляем в базе для будущих запросов
                            try:
                                cur.execute("""
                                    UPDATE ARTISTS 
                                    SET IMAGE_URL = %s 
                                    WHERE ARTIST_ID = %s
                                """, (image_url, artist_id))
                                conn.commit()
                                log_snowflake(f"get_top_artists_from_snowflake: обновлено изображение для артиста {artist_id}")
                            except Exception as update_err:
                                log_snowflake(f"get_top_artists_from_snowflake: ошибка обновления изображения: {update_err}")
                except Exception as api_err:
                    log_snowflake(f"get_top_artists_from_snowflake: ошибка запроса к Spotify API: {api_err}")
            
            data.append({
                'artist': artist_name,
                'count': count,
                'image_url': image_url
            })
        
        log_snowflake(f"get_top_artists_from_snowflake: найдено артистов={len(data)}, с изображениями={sum(1 for d in data if d['image_url'])}")
        return data
    except Exception as e:
        log_snowflake(f"get_top_artists_from_snowflake: ОШИБКА {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()

def get_popularity_distribution_from_snowflake(user_spotify_id):
    """Возвращает распределение популярности треков (0-100)"""
    if not user_spotify_id:
        log_snowflake("get_popularity_distribution_from_snowflake: не указан user_spotify_id")
        return []
    log_snowflake(f"get_popularity_distribution_from_snowflake: старт, user_spotify_id={user_spotify_id}")
    try:
        conn = snowflake.connector.connect(**SNOWFLAKE_CONFIG)
        cur = conn.cursor()
        cur.execute("USE SCHEMA PUBLIC")
        cur.execute("""
            SELECT 
                CASE 
                    WHEN t.POPULARITY IS NULL THEN 0
                    WHEN t.POPULARITY < 20 THEN 0
                    WHEN t.POPULARITY < 40 THEN 20
                    WHEN t.POPULARITY < 60 THEN 40
                    WHEN t.POPULARITY < 80 THEN 60
                    WHEN t.POPULARITY < 100 THEN 80
                    ELSE 100
                END as popularity_range,
                COUNT(DISTINCT lh.TRACK_ID) as track_count
            FROM LISTENING_HISTORY lh
            JOIN TRACKS t ON lh.TRACK_ID = t.TRACK_ID
            WHERE lh.USER_SPOTIFY_ID = %s
            GROUP BY popularity_range
            ORDER BY popularity_range
        """, (user_spotify_id,))
        results = cur.fetchall()
        data = [{'range': f"{row[0]}-{row[0]+19 if row[0] < 100 else 100}", 'count': int(row[1])} for row in results]
        log_snowflake(f"get_popularity_distribution_from_snowflake: найдено диапазонов={len(data)}")
        return data
    except Exception as e:
        log_snowflake(f"get_popularity_distribution_from_snowflake: ОШИБКА {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()

def get_activity_heatmap_from_snowflake(user_spotify_id):
    """Возвращает данные для тепловой карты активности (дни недели × часы дня)"""
    if not user_spotify_id:
        log_snowflake("get_activity_heatmap_from_snowflake: не указан user_spotify_id")
        return []
    log_snowflake(f"get_activity_heatmap_from_snowflake: старт, user_spotify_id={user_spotify_id}")
    try:
        conn = snowflake.connector.connect(**SNOWFLAKE_CONFIG)
        cur = conn.cursor()
        cur.execute("USE SCHEMA PUBLIC")
        cur.execute("""
            SELECT 
                DAYOFWEEKISO(lh.PLAYED_AT) as weekday,
                EXTRACT(HOUR FROM lh.PLAYED_AT) as hour,
                COUNT(*) as play_count
            FROM LISTENING_HISTORY lh
            WHERE lh.USER_SPOTIFY_ID = %s
            GROUP BY weekday, hour
            ORDER BY weekday, hour
        """, (user_spotify_id,))
        results = cur.fetchall()
        # Преобразуем в формат для heatmap: {weekday: {hour: count}}
        data = {}
        weekday_names = {1: 'Пн', 2: 'Вт', 3: 'Ср', 4: 'Чт', 5: 'Пт', 6: 'Сб', 7: 'Вс'}
        for row in results:
            weekday = int(row[0])
            hour = int(row[1])
            count = int(row[2])
            weekday_name = weekday_names.get(weekday, f'День {weekday}')
            if weekday_name not in data:
                data[weekday_name] = {}
            data[weekday_name][hour] = count
        log_snowflake(f"get_activity_heatmap_from_snowflake: найдено записей={len(results)}")
        return data
    except Exception as e:
        log_snowflake(f"get_activity_heatmap_from_snowflake: ОШИБКА {e}")
        import traceback
        traceback.print_exc()
        return {}
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()

def get_listening_activity_by_weekday_from_snowflake(user_spotify_id):
    """Получает активность прослушиваний по дням недели (Пн–Вс) для line chart с заливкой."""
    if not user_spotify_id:
        return []
    try:
        conn = snowflake.connector.connect(**SNOWFLAKE_CONFIG)
        cur = conn.cursor()
        cur.execute("USE SCHEMA PUBLIC")
        cur.execute("""
            SELECT DAYOFWEEKISO(PLAYED_AT::TIMESTAMP_NTZ) AS dow,
                   COUNT(*) AS play_count
            FROM LISTENING_HISTORY
            WHERE USER_SPOTIFY_ID = %s
            GROUP BY dow
            ORDER BY dow
        """, (user_spotify_id,))
        results = cur.fetchall()
        print(f"DEBUG: weekday results = {results}")
        raw = {int(row[0]): int(row[1]) for row in results if row[0] is not None}
        order = [1, 2, 3, 4, 5, 6, 7]  # Пн..Вс
        labels = {
            1: "Пн",
            2: "Вт",
            3: "Ср",
            4: "Чт",
            5: "Пт",
            6: "Сб",
            7: "Вс",
        }
        result = [{'weekday': labels[i], 'count': raw.get(i, 0)} for i in order]
        if not any(item['count'] > 0 for item in result):
            return []
        return result
    except Exception as e:
        print(f"Snowflake Error (get_listening_activity_by_weekday): {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()

# ==========================================
# 4. МАРШРУТЫ (AUTH & APP)
# ==========================================

@app.route("/")
def index():
    return render_template("index.html")

# --- ЛОГИН / РЕГИСТРАЦИЯ ПО ПАРОЛЮ ---

@app.route("/register", methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        display_name = request.form['display_name']
        password = request.form['password']
        email = request.form.get('email', '').strip().lower()

        if len(password) < 6:
            flash("Пароль должен быть не менее 6 символов", "error")
            return render_template("register.html")

        if not email or '@' not in email:
            flash("Введите корректный email адрес", "error")
            return render_template("register.html")

        try:
            user = create_local_user(username, password, display_name, email)
            
            code = generate_verification_code()
            save_verification_code(user['id'], email, code, 'registration')
            send_verification_email(email, code, 'registration')
            
            session['pending_user_id'] = user['id']
            session['pending_email'] = email
            session['pending_purpose'] = 'registration'
            
            flash(f"Код подтверждения отправлен на {email}", "success")
            return redirect('/verify-code')
        except UniqueViolation as e:
            if 'username' in str(e):
                flash("Пользователь с таким логином уже существует", "error")
            elif 'email' in str(e):
                flash("Пользователь с таким email уже существует", "error")
            else:
                flash("Пользователь с такими данными уже существует", "error")
        except Exception as e:
            flash(f"Ошибка регистрации: {e}", "error")

    return render_template("register.html")

@app.route("/login-local", methods=['GET', 'POST'])
def login_local():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        user = get_user_by_username(username)

        if user and user['password_hash'] and check_password_hash(user['password_hash'], password):
            if user['is_blocked']:
                flash("Ваш аккаунт заблокирован", "error")
                return redirect('/login-local')

            if not user.get('email'):
                flash("У вашего аккаунта нет email. Пожалуйста, обратитесь к администратору.", "error")
                return redirect('/login-local')

            code = generate_verification_code()
            save_verification_code(user['id'], user['email'], code, 'login')
            send_verification_email(user['email'], code, 'login')
            
            session['pending_user_id'] = user['id']
            session['pending_email'] = user['email']
            session['pending_purpose'] = 'login'
            
            flash(f"Код подтверждения отправлен на {user['email']}", "success")
            return redirect('/verify-code')
        else:
            flash("Неверный логин или пароль", "error")

    return render_template("login.html")

@app.route("/verify-code", methods=['GET', 'POST'])
def verify_code_page():
    pending_user_id = session.get('pending_user_id')
    pending_email = session.get('pending_email')
    
    if request.method == 'POST':
        code = request.form.get('code', '').strip()
        email = pending_email or request.form.get('email', '').strip()
        purpose = session.get('pending_purpose', 'login')
        
        # Если сессия истекла, но есть код и email, пытаемся найти пользователя по email
        if not pending_user_id and email:
            # Пытаемся найти по email
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            try:
                cur.execute("SELECT * FROM users WHERE email = %s", (email,))
                user = cur.fetchone()
                if user:
                    pending_user_id = user['id']
                    session['pending_user_id'] = pending_user_id
                    session['pending_email'] = email
                    session.permanent = True  # Делаем сессию постоянной
            except:
                pass
            finally:
                cur.close()
                conn.close()
        
        if not pending_user_id or not email:
            flash("Сессия истекла. Пожалуйста, начните заново.", "error")
            return redirect('/login-local')
        
        if verify_code(email, code, purpose):
            user_id = pending_user_id
            user = get_user_by_id(user_id)
            
            if not user:
                flash("Ошибка: пользователь не найден", "error")
                session.clear()
                return redirect('/login-local')
            
            if user['is_blocked']:
                flash("Ваш аккаунт заблокирован", "error")
                session.clear()
                return redirect('/login-local')
            
            if purpose == 'registration':
                conn = get_db_connection()
                cur = conn.cursor()
                try:
                    cur.execute("UPDATE users SET email_verified = TRUE WHERE id = %s", (user_id,))
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    print(f"Ошибка обновления email_verified: {e}")
                finally:
                    cur.close()
                    conn.close()
            
            session.pop('pending_user_id', None)
            session.pop('pending_email', None)
            session.pop('pending_purpose', None)
            
            session['user_db_id'] = user_id
            if user['spotify_id']:
                session['spotify_user_id'] = user['spotify_id']

            session_token = str(user_id) + '_' + str(datetime.now().timestamp()) + '_' + str(random.randint(1000, 9999))
            ip_address = request.remote_addr
            user_agent = request.headers.get('User-Agent')
            create_user_session(user_id, session_token, ip_address, user_agent)
            session['_session_token'] = session_token
            
            log_action(user_id, "LOGIN_LOCAL" if purpose == 'login' else "REGISTER_LOCAL", 
                      "Вход по паролю" if purpose == 'login' else "Регистрация по паролю")
            
            if user['role'] == 'admin':
                return redirect('/admin')
            return redirect('/dashboard')
        else:
            flash("Неверный код подтверждения или код истек", "error")
    
    if not pending_user_id or not pending_email:
        flash("Сессия истекла. Пожалуйста, начните заново.", "error")
        return redirect('/login-local')
    
    email = pending_email

    if '@' in email:
        parts = email.split('@')
        masked_email = parts[0][0] + '*' * (len(parts[0]) - 2) + parts[0][-1] + '@' + parts[1]
    else:
        masked_email = email
    
    return render_template("verify_code.html", email=masked_email)

@app.route("/resend-code", methods=['POST'])
def resend_code():
    pending_user_id = session.get('pending_user_id')
    pending_email = session.get('pending_email')
    
    if not pending_user_id or not pending_email:
        email = request.json.get('email', '') if request.is_json else request.form.get('email', '')
        if email:
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            try:
                cur.execute("SELECT * FROM users WHERE email = %s", (email,))
                user = cur.fetchone()
                if user:
                    pending_user_id = user['id']
                    pending_email = email
                    session['pending_user_id'] = pending_user_id
                    session['pending_email'] = pending_email
                    session.permanent = True
            except:
                pass
            finally:
                cur.close()
                conn.close()
    
    if not pending_user_id or not pending_email:
        return jsonify({'success': False, 'message': 'Сессия истекла. Пожалуйста, начните заново.'}), 400
    
    email = pending_email
    user_id = pending_user_id
    purpose = session.get('pending_purpose', 'login')
    
    code = generate_verification_code()
    save_verification_code(user_id, email, code, purpose)
    send_verification_email(email, code, purpose)
    
    return jsonify({'success': True, 'message': f'Код отправлен на {email}'})

@app.route("/profile", methods=['GET', 'POST'])
def profile():
    """Страница профиля пользователя"""
    if 'user_db_id' not in session:
        flash("Пожалуйста, войдите в систему", "error")
        return redirect('/login-local')
    
    user_id = session['user_db_id']
    user = get_user_by_id(user_id)
    
    if not user:
        flash("Пользователь не найден", "error")
        session.clear()
        return redirect('/login-local')
    
    preferences = get_user_preferences(user_id)
    
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'update_password':
            current_password = request.form.get('current_password', '').strip()
            new_password = request.form.get('new_password', '').strip()
            confirm_password = request.form.get('confirm_password', '').strip()
            
            if not current_password or not new_password or not confirm_password:
                flash("Все поля обязательны для заполнения", "error")
            elif new_password != confirm_password:
                flash("Новые пароли не совпадают", "error")
            elif len(new_password) < 6:
                flash("Пароль должен содержать минимум 6 символов", "error")
            elif not user.get('password_hash') or not check_password_hash(user['password_hash'], current_password):
                flash("Текущий пароль неверен", "error")
            else:
                if update_user_password(user_id, new_password, changed_by_user_id=user_id):
                    log_action(user_id, "UPDATE_PASSWORD", "Пароль обновлен")
                    flash("Пароль успешно обновлен", "success")
                else:
                    flash("Ошибка при обновлении пароля", "error")
        
        elif action == 'update_email':
            new_email = request.form.get('new_email', '').strip()
            
            if not new_email or '@' not in new_email:
                flash("Введите корректный email адрес", "error")
            else:
                conn = get_db_connection()
                cur = conn.cursor(cursor_factory=RealDictCursor)
                try:
                    cur.execute("SELECT id FROM users WHERE email = %s AND id != %s", (new_email, user_id))
                    if cur.fetchone():
                        flash("Этот email уже используется другим пользователем", "error")
                    else:
                        if update_user_email(user_id, new_email, changed_by_user_id=user_id):
                            log_action(user_id, "UPDATE_EMAIL", f"Email обновлен на {new_email}")
                            flash("Email успешно обновлен. Пожалуйста, подтвердите новый email", "success")
                            user = get_user_by_id(user_id)
                        else:
                            flash("Ошибка при обновлении email", "error")
                finally:
                    cur.close()
                    conn.close()
        
        elif action == 'update_display_name':
            new_display_name = request.form.get('new_display_name', '').strip()
            
            if not new_display_name:
                flash("Имя не может быть пустым", "error")
            elif len(new_display_name) > 100:
                flash("Имя слишком длинное (максимум 100 символов)", "error")
            else:
                if update_user_display_name(user_id, new_display_name):
                    log_action(user_id, "UPDATE_DISPLAY_NAME", f"Имя обновлено на {new_display_name}")
                    flash("Имя успешно обновлено", "success")
                    user = get_user_by_id(user_id)
                else:
                    flash("Ошибка при обновлении имени", "error")
        
        elif action == 'send_verification_code':
            if not user.get('email'):
                flash("У вас не указан email адрес", "error")
            else:
                code = generate_verification_code()
                save_verification_code(user_id, user['email'], code, 'email_verification')
                if send_verification_email(user['email'], code, 'registration'):
                    log_action(user_id, "SEND_VERIFICATION_CODE", "Код подтверждения отправлен на email")
                    flash(f"Код подтверждения отправлен на {user['email']}", "success")
                else:
                    flash("Ошибка отправки кода. Проверьте настройки email.", "error")
        
        elif action == 'verify_email_code':
            code = request.form.get('verification_code', '').strip()
            if not code:
                flash("Введите код подтверждения", "error")
            elif not user.get('email'):
                flash("У вас не указан email адрес", "error")
            else:
                if verify_code(user['email'], code, 'email_verification'):
                    conn = get_db_connection()
                    cur = conn.cursor()
                    try:
                        cur.execute("UPDATE users SET email_verified = TRUE WHERE id = %s", (user_id,))
                        conn.commit()
                        log_action(user_id, "VERIFY_EMAIL", "Email подтвержден")
                        flash("Email успешно подтвержден!", "success")
                        user = get_user_by_id(user_id)
                    except Exception as e:
                        conn.rollback()
                        print(f"Ошибка подтверждения email: {e}")
                        flash("Ошибка при подтверждении email", "error")
                    finally:
                        cur.close()
                        conn.close()
                else:
                    flash("Неверный код подтверждения или код истек", "error")
        
        return redirect('/profile')
    
    return render_template('profile.html', user=user, preferences=preferences)

@app.route("/feedback", methods=['GET', 'POST'])
def feedback():
    """Страница для отправки обратной связи"""
    user_id = session.get('user_db_id')
    user = get_user_by_id(user_id) if user_id else None
    
    if request.method == 'POST':
        feedback_type = request.form.get('feedback_type', '').strip()
        title = request.form.get('title', '').strip()
        message = request.form.get('message', '').strip()
        page_url = request.form.get('page_url', '').strip() or request.referrer
        
        if not feedback_type or not title or not message:
            flash("Пожалуйста, заполните все обязательные поля", "error")
            return render_template('feedback.html', user=user)
        
        if feedback_type not in ['bug', 'feature_request', 'complaint', 'praise', 'other']:
            flash("Неверный тип обратной связи", "error")
            return render_template('feedback.html', user=user)
        
        result = create_feedback(user_id, feedback_type, title, message, page_url, created_by_user_id=user_id)
        
        if result:
            if user_id:
                log_action(user_id, "SUBMIT_FEEDBACK", f"Отправлен feedback: {feedback_type}")
            flash("Спасибо за вашу обратную связь! Мы обязательно рассмотрим ваше сообщение.", "success")
            return redirect('/feedback')
        else:
            flash("Ошибка при отправке обратной связи. Попробуйте позже.", "error")
    
    return render_template('feedback.html', user=user)
    
    return render_template("profile.html", user=user, preferences=preferences)

# --- OAUTH SPOTIFY ---

@app.route("/forgot-password", methods=['GET', 'POST'])
def forgot_password():
    """Страница для запроса восстановления пароля"""
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        
        if not email:
            flash('Пожалуйста, введите email адрес', 'error')
            return render_template('forgot_password.html')
        
        # Ищем пользователя по email
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cur.execute("SELECT id, username FROM users WHERE email = %s", (email,))
            user = cur.fetchone()
            
            if user:
                # Проверяем, что у пользователя есть пароль (не только Spotify-авторизация)
                cur.execute("SELECT password_hash FROM users WHERE id = %s", (user['id'],))
                user_data = cur.fetchone()
                
                if user_data and user_data['password_hash']:
                    # Генерируем токен
                    token = generate_password_reset_token(user['id'])
                    
                    # Формируем URL для сброса пароля
                    reset_url = request.url_root.rstrip('/') + f'/reset-password/{token}'
                    
                    # Отправляем email
                    if send_password_reset_email(email, reset_url):
                        flash('Письмо с инструкциями по восстановлению пароля отправлено на ваш email', 'success')
                        log_action(user['id'], "REQUEST_PASSWORD_RESET", f"Запрошено восстановление пароля для {email}")
                    else:
                        flash('Ошибка при отправке email. Попробуйте позже или обратитесь к администратору.', 'error')
                else:
                    # Пользователь авторизуется только через Spotify
                    flash('Этот аккаунт не имеет пароля. Используйте вход через Spotify.', 'info')
            else:
                # Не сообщаем, что пользователя нет (для безопасности)
                flash('Если пользователь с таким email существует, письмо с инструкциями отправлено', 'success')
        except Exception as e:
            print(f"Ошибка при обработке запроса восстановления пароля: {e}")
            flash('Произошла ошибка. Попробуйте позже.', 'error')
        finally:
            cur.close()
            conn.close()
        
        return render_template('forgot_password.html')
    
    return render_template('forgot_password.html')

@app.route("/reset-password/<token>", methods=['GET', 'POST'])
def reset_password(token):
    """Страница для сброса пароля по токену"""
    if request.method == 'POST':
        new_password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        if not new_password:
            flash('Введите новый пароль', 'error')
            return render_template('reset_password.html', token=token, valid=True)
        
        if len(new_password) < 6:
            flash('Пароль должен содержать не менее 6 символов', 'error')
            return render_template('reset_password.html', token=token, valid=True)
        
        if new_password != confirm_password:
            flash('Пароли не совпадают', 'error')
            return render_template('reset_password.html', token=token, valid=True)
        
        # Проверяем токен
        user_id = verify_password_reset_token(token)
        
        if user_id:
            # Обновляем пароль
            conn = get_db_connection()
            cur = conn.cursor()
            try:
                password_hash = generate_password_hash(new_password)
                cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user_id))
                conn.commit()
                
                flash('Пароль успешно изменен. Теперь вы можете войти с новым паролем.', 'success')
                log_action(user_id, "PASSWORD_RESET", "Пароль успешно изменен через восстановление")
                return redirect('/login')
            except Exception as e:
                conn.rollback()
                print(f"Ошибка при сбросе пароля: {e}")
                flash('Произошла ошибка при изменении пароля. Попробуйте позже.', 'error')
            finally:
                cur.close()
                conn.close()
        else:
            flash('Недействительная или истекшая ссылка для восстановления пароля. Запросите новую.', 'error')
            return render_template('reset_password.html', token=token, valid=False)
    
    # GET запрос - показываем форму
    # Проверяем токен для отображения формы
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    valid = False
    try:
        cur.execute("""
            SELECT user_id FROM password_reset_tokens
            WHERE token = %s AND used = FALSE AND expires_at > NOW()
            LIMIT 1
        """, (token,))
        result = cur.fetchone()
        valid = result is not None
    except:
        pass
    finally:
        cur.close()
        conn.close()
    
    return render_template('reset_password.html', token=token, valid=valid)

@app.route("/login-spotify")
def login_spotify():
    params = {'client_id': CLIENT_ID, 'response_type': 'code', 'redirect_uri': REDIRECT_URI, 'scope': SCOPE, 'show_dialog': True}
    return redirect(f"{AUTH_URL}?{urlencode(params)}")

@app.route("/callback")
def callback():
    code = request.args.get('code')
    if not code:
        flash("Ошибка авторизации Spotify. Попробуйте войти снова.", "error")
        if 'user_db_id' in session:
            return redirect('/dashboard')
        return redirect('/')

    auth_str = f"{CLIENT_ID}:{CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    resp = requests.post(TOKEN_URL, data={'grant_type': 'authorization_code', 'code': code, 'redirect_uri': REDIRECT_URI}, headers={'Authorization': f'Basic {b64_auth}'})
    token_info = resp.json()

    if 'access_token' not in token_info:
        flash("Ошибка получения токена авторизации. Попробуйте войти снова.", "error")
        if 'user_db_id' in session:
            return redirect('/dashboard')
        return redirect('/')

    session['access_token'] = token_info['access_token']
    if 'refresh_token' in token_info:
        session['refresh_token'] = token_info['refresh_token']

    headers = {'Authorization': f'Bearer {session["access_token"]}'}
    sp_user_resp = requests.get(API_BASE_URL + 'me', headers=headers)

    if sp_user_resp.status_code != 200:
        flash("Ошибка получения профиля Spotify. Попробуйте войти снова.", "error")
        if 'user_db_id' in session:
            return redirect('/dashboard')
        return redirect('/')

    sp_user = sp_user_resp.json()
    sp_id = sp_user['id']

    if 'user_db_id' in session:
        user = get_user_by_id(session['user_db_id'])

        if not user['spotify_id']:
            try:
                link_spotify_to_user(user['id'], sp_id)

                log_action(user['id'], "LINK_SPOTIFY", f"Привязал Spotify {sp_id}")
                flash("Spotify успешно привязан!", "success")

            except UniqueViolation:
                flash("Ошибка: Этот аккаунт Spotify уже привязан к другому пользователю!", "error")

                session.pop('access_token', None)

                if user['role'] == 'admin':
                    return redirect('/admin')
                return redirect('/dashboard')

        elif user['spotify_id'] != sp_id:
            flash("К вашему профилю уже привязан другой Spotify аккаунт. Зайдите в него.", "error")
            if user['role'] == 'admin':
                return redirect('/admin')
            return redirect('/dashboard')

        session['spotify_user_id'] = sp_id
        
        session_token = str(user['id']) + '_' + str(datetime.now().timestamp()) + '_' + str(random.randint(1000, 9999))
        ip_address = request.remote_addr
        user_agent = request.headers.get('User-Agent')
        create_user_session(user['id'], session_token, ip_address, user_agent)
        session['_session_token'] = session_token
        
        try:
            headers = {'Authorization': f'Bearer {session["access_token"]}'}
            rr = requests.get(API_BASE_URL + 'me/player/recently-played?limit=50', headers=headers, timeout=5)
            if rr.status_code == 200:
                all_recent_tracks = rr.json().get('items', [])
                save_tracks_to_snowflake_background(sp_id, all_recent_tracks, session.get('access_token'))
        except:
            pass
        
        if user['role'] == 'admin':
            return redirect('/admin')
        return redirect('/dashboard')

    db_user = get_user_by_spotify_id(sp_id)
    if not db_user:
        try:
            db_user = create_spotify_user(sp_id, sp_user.get('display_name', 'Spotify User'))
            log_action(db_user['id'], "REGISTER_SPOTIFY", "Регистрация через OAuth")
        except UniqueViolation:
            db_user = get_user_by_spotify_id(sp_id)

    if db_user['is_blocked']:
        return "Ваш аккаунт заблокирован", 403

    session['user_db_id'] = db_user['id']
    session['spotify_user_id'] = db_user['spotify_id']
    
    session_token = str(db_user['id']) + '_' + str(datetime.now().timestamp()) + '_' + str(random.randint(1000, 9999))
    ip_address = request.remote_addr
    user_agent = request.headers.get('User-Agent')
    create_user_session(db_user['id'], session_token, ip_address, user_agent)
    session['_session_token'] = session_token
    
    log_action(db_user['id'], "LOGIN_SPOTIFY", "Вход через Spotify")

    try:
        headers = {'Authorization': f'Bearer {session["access_token"]}'}
        rr = requests.get(API_BASE_URL + 'me/player/recently-played?limit=50', headers=headers, timeout=5)
        if rr.status_code == 200:
            all_recent_tracks = rr.json().get('items', [])
            save_tracks_to_snowflake_background(sp_id, all_recent_tracks, session.get('access_token'))
    except:
        pass

    if db_user['role'] == 'admin':
        return redirect("/admin")
    return redirect("/dashboard")

    # --- DASHBOARD ---

@app.route("/dashboard")
def dashboard():
    """Страница дашборда - рендерится сразу, данные загружаются асинхронно"""
    if 'user_db_id' not in session: return redirect("/")
    user = get_user_by_id(session['user_db_id'])

    if user['is_blocked']:
        session.clear()
        return "Ваш аккаунт заблокирован"

    log_action(user['id'], "VIEW_DASHBOARD", "Просмотр")
    # Рендерим страницу сразу без данных - они загрузятся через AJAX
    return render_template(
        "dashboard.html",
        user=user,
        tracks=[],
        top_tracks=[],
        user_vibe=None,
        activity_by_hour=[],
        genre_distribution=[],
        activity_by_weekday=[],
    )

@app.route("/api/dashboard/data")
def api_dashboard_data():
    """API endpoint для асинхронной загрузки данных дашборда"""
    if 'user_db_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    user = get_user_by_id(session['user_db_id'])
    if not user:
        return jsonify({'error': 'User not found'}), 404

    recent_tracks = []
    top_tracks = []
    activity_by_hour = []
    genre_distribution = []
    activity_by_weekday = []

    # Загружаем недавние треки из Spotify (если есть токен)
    if session.get('access_token') and user.get('spotify_id'):
        headers = {'Authorization': f'Bearer {session["access_token"]}'}
        try:
            rr = requests.get(API_BASE_URL + 'me/player/recently-played?limit=50', headers=headers, timeout=5)
            if rr.status_code == 200:
                all_recent_tracks = rr.json().get('items', [])
                recent_tracks = all_recent_tracks[:5]
                # Сохраняем в фоне (не ждем)
                save_tracks_to_snowflake_background(user['spotify_id'], all_recent_tracks, session.get('access_token'))
            elif rr.status_code == 401:
                session.pop('access_token', None)
        except Exception as e:
            print(f"Ошибка загрузки недавних треков: {e}")

    # Загружаем данные из Snowflake
    user_vibe = "Музыкальный исследователь"
    top_artists = []
    activity_heatmap = {}
    total_minutes_listened = 0
    summary = {
        'total_minutes_listened': 0,
        'favorite_genre': None,
        'favorite_artist': None,
        'favorite_track': None,
    }
    if user.get('spotify_id'):
        try:
            activity_by_hour = get_listening_activity_by_hour_from_snowflake(user['spotify_id'])
            genre_distribution = get_genre_distribution_from_snowflake(user['spotify_id'], limit=5)
            activity_by_weekday = get_listening_activity_by_weekday_from_snowflake(user['spotify_id'])
            top_tracks = get_top_tracks_from_snowflake(user['spotify_id'], limit=5)
            user_vibe = generate_user_vibe(user['spotify_id'])
            top_artists = get_top_artists_from_snowflake(user['spotify_id'], limit=5, access_token=session.get('access_token'))
            activity_heatmap = get_activity_heatmap_from_snowflake(user['spotify_id'])
            # Сводная информация
            total_minutes_listened = get_total_minutes_listened_from_snowflake(user['spotify_id'])
            favorite_genre = genre_distribution[0]['genre'] if genre_distribution else None
            favorite_artist = top_artists[0]['artist'] if top_artists else None
            favorite_track = top_tracks[0]['name'] if top_tracks else None
            summary = {
                'total_minutes_listened': total_minutes_listened,
                'favorite_genre': favorite_genre,
                'favorite_artist': favorite_artist,
                'favorite_track': favorite_track,
            }
        except Exception as e:
            print(f"Ошибка загрузки данных из Snowflake: {e}")

    return jsonify({
        'recent_tracks': recent_tracks,
        'top_tracks': top_tracks,
        'user_vibe': user_vibe,
        'activity_by_hour': activity_by_hour,
        'genre_distribution': genre_distribution,
        'activity_by_weekday': activity_by_weekday,
        'top_artists': top_artists,
        'activity_heatmap': activity_heatmap,
        'summary': summary,
    })

@app.route("/dashboard/send-report", methods=['POST'])
def send_dashboard_report():
    """Отправляет отчет дашборда на email пользователя"""
    if 'user_db_id' not in session:
        return jsonify({'success': False, 'message': 'Необходима авторизация'}), 401
    
    user = get_user_by_id(session['user_db_id'])
    
    if not user:
        return jsonify({'success': False, 'message': 'Пользователь не найден'}), 404
    
    if not user.get('email'):
        return jsonify({'success': False, 'message': 'У вас не указан email. Пожалуйста, обновите профиль.'}), 400
    
    if not user.get('email_verified'):
        return jsonify({'success': False, 'message': 'Email не подтвержден. Пожалуйста, подтвердите email.'}), 400
    
    recent_tracks = []
    top_tracks = []
    activity_by_hour = []
    genre_distribution = []
    activity_by_weekday = []
    top_artists = []
    summary = None
    user_vibe = "Музыкальный исследователь"
    
    if session.get('access_token') and user.get('spotify_id'):
        headers = {'Authorization': f'Bearer {session["access_token"]}'}
        try:
            rr = requests.get(API_BASE_URL + 'me/player/recently-played?limit=50', headers=headers)
            if rr.status_code == 200:
                all_recent_tracks = rr.json().get('items', [])
                recent_tracks = all_recent_tracks[:5]
        except:
            pass
    
    if user.get('spotify_id'):
        spotify_id = user['spotify_id']
        activity_by_hour = get_listening_activity_by_hour_from_snowflake(spotify_id)
        genre_distribution = get_genre_distribution_from_snowflake(spotify_id, limit=5)
        activity_by_weekday = get_listening_activity_by_weekday_from_snowflake(spotify_id)
        top_tracks = get_top_tracks_from_snowflake(spotify_id, limit=5)
        user_vibe = generate_user_vibe(spotify_id)
        top_artists = get_top_artists_from_snowflake(spotify_id, limit=5, access_token=session.get('access_token'))
        total_minutes_listened = get_total_minutes_listened_from_snowflake(spotify_id)
        favorite_genre = genre_distribution[0]['genre'] if genre_distribution else None
        favorite_artist = top_artists[0]['artist'] if top_artists else None
        favorite_track = top_tracks[0]['name'] if top_tracks else None
        summary = {
            'total_minutes_listened': total_minutes_listened,
            'favorite_genre': favorite_genre,
            'favorite_artist': favorite_artist,
            'favorite_track': favorite_track,
        }
    
    # Отправляем отчет
    success, message = send_dashboard_report_email(
        user_email=user['email'],
        user=user,
        recent_tracks=recent_tracks,
        top_tracks=top_tracks,
        genre_distribution=genre_distribution,
        activity_by_hour=activity_by_hour,
        activity_by_weekday=activity_by_weekday,
        user_vibe=user_vibe,
        top_artists=top_artists,
        summary=summary,
    )
    
    if success:
        log_action(user['id'], "SEND_REPORT", "Отправлен отчет на email")
        return jsonify({'success': True, 'message': message})
    else:
        return jsonify({'success': False, 'message': message}), 500

@app.route("/recommendations")
def recommendations():
    """Страница с рекомендациями - рендерится сразу, данные загружаются асинхронно"""
    if 'user_db_id' not in session:
        return redirect("/")
    
    user = get_user_by_id(session['user_db_id'])
    
    if user['is_blocked']:
        session.clear()
        return "Ваш аккаунт заблокирован"
    
    log_action(user['id'], "VIEW_RECOMMENDATIONS", "Просмотр рекомендаций")
    # Рендерим страницу сразу без данных - они загрузятся через AJAX
    return render_template(
        "recommendations.html",
        user=user,
        recommended_tracks=[],
        recommended_artists=[],
        user_vibe=None
    )

@app.route("/api/recommendations/data")
def api_recommendations_data():
    """API endpoint для асинхронной загрузки рекомендаций"""
    if 'user_db_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    user = get_user_by_id(session['user_db_id'])
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    recommended_tracks = []
    recommended_artists = []
    user_vibe = "Музыкальный исследователь"
    
    if user.get('spotify_id'):
        try:
            recommended_tracks = get_recommendations_from_snowflake(user['spotify_id'], limit=10, min_limit=10)
            recommended_artists = get_recommended_artists_from_snowflake(user['spotify_id'], limit=5)
            user_vibe = generate_user_vibe(user['spotify_id'])
        except Exception as e:
            print(f"Ошибка загрузки рекомендаций: {e}")
    
    return jsonify({
        'recommended_tracks': recommended_tracks,
        'recommended_artists': recommended_artists,
        'user_vibe': user_vibe
    })

@app.route("/api/track-info/<track_id>")
def get_track_info_api(track_id):
    """API endpoint для получения полной информации о треке"""
    if 'user_db_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    track_info = get_track_full_info_from_snowflake(track_id)
    
    if session.get('access_token') and track_id:
        headers = {'Authorization': f'Bearer {session["access_token"]}'}
        try:
            track_resp = requests.get(API_BASE_URL + f'tracks/{track_id}', headers=headers)
            if track_resp.status_code == 200:
                spotify_data = track_resp.json()
                if track_info:
                    track_info['spotify_url'] = spotify_data.get('external_urls', {}).get('spotify')
                    track_info['preview_url'] = spotify_data.get('preview_url')
                    track_info['album_name'] = spotify_data.get('album', {}).get('name')
                    track_info['album_release_date'] = spotify_data.get('album', {}).get('release_date')
                    track_info['artists_full'] = [{'name': a['name'], 'id': a['id']} for a in spotify_data.get('artists', [])]
                else:
                    track_info = {
                        'track_id': track_id,
                        'name': spotify_data.get('name'),
                        'artist': spotify_data.get('artists', [{}])[0].get('name') if spotify_data.get('artists') else None,
                        'duration_ms': spotify_data.get('duration_ms'),
                        'popularity': spotify_data.get('popularity'),
                        'image_url': spotify_data.get('album', {}).get('images', [{}])[0].get('url') if spotify_data.get('album', {}).get('images') else None,
                        'spotify_url': spotify_data.get('external_urls', {}).get('spotify'),
                        'preview_url': spotify_data.get('preview_url'),
                        'album_name': spotify_data.get('album', {}).get('name'),
                        'album_release_date': spotify_data.get('album', {}).get('release_date'),
                        'artists_full': [{'name': a['name'], 'id': a['id']} for a in spotify_data.get('artists', [])]
                    }
        except Exception as e:
            print(f"Error getting Spotify track info: {e}")
    
    if track_info:
        return jsonify(track_info)
    else:
        return jsonify({'error': 'Track not found'}), 404

def get_artist_info_from_snowflake(artist_id):
    """Получает информацию об исполнителе из Snowflake"""
    if not artist_id:
        return None
    try:
        conn = snowflake.connector.connect(**SNOWFLAKE_CONFIG)
        cur = conn.cursor()
        cur.execute("USE SCHEMA PUBLIC")
        cur.execute("""
            SELECT ARTIST_ID, NAME, GENRES, POPULARITY, FOLLOWERS, IMAGE_URL
            FROM ARTISTS
            WHERE ARTIST_ID = %s
        """, (artist_id,))
        result = cur.fetchone()
        if result:
            genres_raw = result[2]
            genres = []
            if genres_raw:
                # Snowflake возвращает ARRAY как список Python
                if isinstance(genres_raw, list):
                    genres = [str(g).strip() for g in genres_raw if g and str(g).strip() and str(g).strip() != 'None' and str(g).strip() != 'null']
                elif isinstance(genres_raw, str):
                    # Если пришла строка, пытаемся распарсить как JSON массив
                    try:
                        import json
                        # Убираем возможные пробелы и кавычки
                        cleaned = genres_raw.strip()
                        if cleaned.startswith('[') and cleaned.endswith(']'):
                            genres_parsed = json.loads(cleaned)
                            if isinstance(genres_parsed, list):
                                genres = [str(g).strip() for g in genres_parsed if g and str(g).strip() and str(g).strip() != 'None' and str(g).strip() != 'null']
                        else:
                            # Если не JSON, пробуем разделить по запятой
                            genres = [g.strip().strip('"').strip("'") for g in cleaned.split(',') if g.strip()]
                    except:
                        # Если не удалось распарсить, пробуем разделить по запятой
                        genres = [g.strip().strip('"').strip("'") for g in genres_raw.split(',') if g.strip()]
                elif hasattr(genres_raw, '__iter__') and not isinstance(genres_raw, (str, bytes)):
                    # Итерируемый объект (но не строка)
                    try:
                        genres = [str(g).strip() for g in genres_raw if g and str(g).strip() and str(g).strip() != 'None' and str(g).strip() != 'null']
                    except:
                        genres = []
            # Убираем дубликаты и пустые значения
            genres = list(dict.fromkeys([g for g in genres if g]))
            
            return {
                'artist_id': result[0],
                'name': result[1],
                'genres': genres,
                'popularity': result[3],
                'followers': result[4],
                'image_url': result[5]
            }
        return None
    except Exception as e:
        print(f"Snowflake Error (get_artist_info): {e}")
        return None
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()

@app.route("/api/artist-info/<artist_id>")
def get_artist_info_api(artist_id):
    """API endpoint для получения полной информации об исполнителе"""
    if 'user_db_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    artist_info = get_artist_info_from_snowflake(artist_id)
    
    if session.get('access_token') and artist_id:
        headers = {'Authorization': f'Bearer {session["access_token"]}'}
        try:
            artist_resp = requests.get(API_BASE_URL + f'artists/{artist_id}', headers=headers)
            if artist_resp.status_code == 200:
                spotify_data = artist_resp.json()
                if artist_info:
                    artist_info['spotify_url'] = spotify_data.get('external_urls', {}).get('spotify')
                    if spotify_data.get('images') and len(spotify_data['images']) > 0:
                        artist_info['image_url'] = spotify_data['images'][0].get('url')
                    if spotify_data.get('genres'):
                        artist_info['genres'] = spotify_data['genres']
                else:
                    artist_info = {
                        'artist_id': artist_id,
                        'name': spotify_data.get('name'),
                        'genres': spotify_data.get('genres', []),
                        'popularity': spotify_data.get('popularity'),
                        'followers': spotify_data.get('followers', {}).get('total'),
                        'image_url': spotify_data.get('images', [{}])[0].get('url') if spotify_data.get('images') else None,
                        'spotify_url': spotify_data.get('external_urls', {}).get('spotify')
                    }
        except Exception as e:
            print(f"Error getting Spotify artist info: {e}")
    
    if artist_info:
        # Если нет spotify_url, строим его из artist_id
        if not artist_info.get('spotify_url') and artist_id:
            artist_info['spotify_url'] = f'https://open.spotify.com/artist/{artist_id}'
        return jsonify(artist_info)
    else:
        return jsonify({'error': 'Artist not found'}), 404

# --- АДМИНКА ---
@app.route("/admin")
@admin_required
def admin_panel():
    search = request.args.get("search", "").strip()
    role_filter = request.args.get("role") or ""
    status_filter = request.args.get("status") or ""
    user_sort_by = request.args.get("user_sort_by", "id")
    user_sort_order = request.args.get("user_sort_order", "asc")
    log_sort_by = request.args.get("log_sort_by", "timestamp")
    log_sort_order = request.args.get("log_sort_order", "desc")
    
    feedback_status = request.args.get("feedback_status", "")
    feedback_type_filter = request.args.get("feedback_type", "")
    feedback_page = int(request.args.get("feedback_page", 1))
    feedback_limit = 20
    feedback_offset = (feedback_page - 1) * feedback_limit
    
    all_feedback = get_all_feedback(
        status=feedback_status if feedback_status else None,
        feedback_type=feedback_type_filter if feedback_type_filter else None,
        limit=feedback_limit,
        offset=feedback_offset
    )
    feedback_count = get_feedback_count()
    new_feedback_count = get_feedback_count('new')

    users = get_all_users(
        search=search or None,
        role=role_filter or None,
        status=status_filter or None,
        sort_by=user_sort_by,
        sort_order=user_sort_order,
    )

    def build_url(**kwargs):
        from urllib.parse import urlencode
        params = request.args.to_dict()
        params.update(kwargs)
        params = {k: v for k, v in params.items() if v}
        return '?' + urlencode(params) if params else ''
    
    return render_template(
        "admin.html",
        users=users,
        logs=get_recent_logs(50, sort_by=log_sort_by, sort_order=log_sort_order),
        search=search,
        role_filter=role_filter,
        status_filter=status_filter,
        user_sort_by=user_sort_by,
        user_sort_order=user_sort_order,
        log_sort_by=log_sort_by,
        log_sort_order=log_sort_order,
        build_url=build_url,
        all_feedback=all_feedback,
        feedback_status=feedback_status,
        feedback_type_filter=feedback_type_filter,
        feedback_page=feedback_page,
        feedback_count=feedback_count,
        new_feedback_count=new_feedback_count,
    )

@app.route("/admin/api/feedback/<int:feedback_id>")
@admin_required
def get_feedback_api(feedback_id):
    """API endpoint для получения feedback по ID"""
    feedback = get_feedback_by_id(feedback_id)
    if feedback:
        return jsonify(dict(feedback))
    else:
        return jsonify({'error': 'Feedback not found'}), 404

@app.route("/admin/api/user-logs/<int:user_id>")
@admin_required
def get_user_logs_api(user_id):
    """API endpoint для получения логов конкретного пользователя"""
    sort_by = request.args.get("sort_by", "timestamp")
    sort_order = request.args.get("sort_order", "desc")
    limit = int(request.args.get("limit", 100))
    
    logs = get_recent_logs(limit=limit, sort_by=sort_by, sort_order=sort_order, user_id=user_id)
    user = get_user_by_id(user_id)
    
    return jsonify({
        'logs': [dict(log) for log in logs],
        'user': dict(user) if user else None
    })

@app.route("/admin/api/user-sessions/<int:user_id>")
@admin_required
def api_user_sessions(user_id):
    """API endpoint для получения сессий конкретного пользователя"""
    sort_by = request.args.get('sort_by', 'created_at')
    sort_order = request.args.get('sort_order', 'desc')
    
    sessions = get_user_sessions(user_id, sort_by=sort_by, sort_order=sort_order)
    return jsonify({'sessions': [dict(session) for session in sessions]})

@app.route("/admin/block/<int:user_id>")
@admin_required
def block_user_route(user_id):
    target = get_user_by_id(user_id)
    if target['role'] == 'admin': return "Нельзя заблокировать админа", 400
    new_s = toggle_block_status(user_id)
    log_action(session['user_db_id'], "BLOCK_USER", f"Пользователь {user_id} статус {new_s}")
    return redirect("/admin")

@app.route("/admin/feedback/update/<int:feedback_id>", methods=['POST'])
@admin_required
def update_feedback_route(feedback_id):
    """Обновление статуса feedback администратором"""
    status = request.form.get('status', '').strip()
    admin_notes = request.form.get('admin_notes', '').strip()
    priority = request.form.get('priority', '').strip() or None
    
    if status and status not in ['new', 'reviewed', 'in_progress', 'resolved', 'rejected']:
        return jsonify({'success': False, 'message': 'Неверный статус'}), 400
    
    admin_user = get_user_by_id(session['user_db_id'])
    if update_feedback_status(feedback_id, status, admin_notes if admin_notes else None, priority, changed_by_user_id=admin_user['id']):
        log_action(admin_user['id'], "UPDATE_FEEDBACK", f"Обновлен feedback #{feedback_id}: {status}")
        return jsonify({'success': True, 'message': 'Статус обновлен'})
    else:
        return jsonify({'success': False, 'message': 'Ошибка обновления'}), 500

@app.route("/admin/feedback/send-email/<int:feedback_id>", methods=['POST'])
@admin_required
def send_feedback_email_route(feedback_id):
    """Отправка ответа на feedback пользователю"""
    feedback = get_feedback_by_id(feedback_id)
    if not feedback:
        return jsonify({'success': False, 'message': 'Feedback не найден'}), 404
    
    if not feedback.get('email'):
        return jsonify({'success': False, 'message': 'У пользователя не указан email'}), 400
    
    custom_message = request.form.get('custom_message', '').strip()
    
    success, message = send_feedback_response_email(feedback, custom_message if custom_message else None)
    
    if success:
        admin_user = get_user_by_id(session['user_db_id'])
        log_action(admin_user['id'], "SEND_FEEDBACK_EMAIL", f"Отправлен ответ на feedback #{feedback_id}")
        return jsonify({'success': True, 'message': message})
    else:
        return jsonify({'success': False, 'message': message}), 500

@app.route("/admin/toggle-admin/<int:user_id>")
@admin_required
def toggle_admin_route(user_id):
    """Переключает роль пользователя между admin и user"""
    target = get_user_by_id(user_id)
    if not target:
        flash("Пользователь не найден", "error")
        return redirect("/admin")
    
    # Нельзя снять роль админа у самого себя
    if user_id == session['user_db_id']:
        flash("Нельзя изменить свою собственную роль", "error")
        return redirect("/admin")
    
    new_role = toggle_admin_role(user_id)
    if new_role:
        action_text = "назначен админом" if new_role == 'admin' else "лишен прав админа"
        log_action(session['user_db_id'], "TOGGLE_ADMIN", f"Пользователь {user_id} {action_text}")
        flash(f"Роль пользователя изменена на '{new_role}'", "success")
    else:
        flash("Ошибка при изменении роли", "error")
    
    return redirect("/admin")

@app.route("/admin/export/excel")
@admin_required
def export_to_excel():
    """Экспорт данных из БД в Excel файл"""
    if not OPENPYXL_AVAILABLE:
        flash("Библиотека OpenPyXL не установлена. Установите её командой: pip install openpyxl", "error")
        return redirect("/admin")
    
    try:
        wb = Workbook()
        wb.remove(wb.active)  # Удаляем дефолтный лист

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        ws_users = wb.create_sheet("Пользователи")
        cur.execute("""
            SELECT id, username, display_name, email, spotify_id, role, 
                   is_blocked, created_at
            FROM users
            ORDER BY id
        """)
        users = cur.fetchall()
        
        headers_users = ["ID", "Логин", "Имя", "Email", "Spotify ID", "Роль", 
                        "Заблокирован", "Дата создания"]
        ws_users.append(headers_users)
        
        header_fill = PatternFill(start_color="1DB954", end_color="1DB954", fill_type="solid")
        header_font = Font(bold=True, color="FFFFFF")
        header_alignment = Alignment(horizontal="center", vertical="center")
        
        for cell in ws_users[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = header_alignment
        
        for user in users:
            ws_users.append([
                user['id'],
                user['username'] or '',
                user['display_name'] or '',
                user['email'] or '',
                user['spotify_id'] or '',
                user['role'],
                'Да' if user['is_blocked'] else 'Нет',
                user['created_at'].strftime('%Y-%m-%d %H:%M:%S') if user['created_at'] else ''
            ])
        
        for column in ws_users.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            adjusted_width = min(max_length + 2, 50)
            ws_users.column_dimensions[column_letter].width = adjusted_width
        
        # 2. Лист с логами аудита
        ws_logs = wb.create_sheet("Логи аудита")
        cur.execute("""
            SELECT l.id, l.timestamp, u.display_name as user_name, u.id as user_id,
                   l.action, l.details
            FROM audit_logs l
            LEFT JOIN users u ON l.user_id = u.id
            ORDER BY l.timestamp DESC
            LIMIT 10000
        """)
        logs = cur.fetchall()
        
        headers_logs = ["ID", "Время", "Пользователь", "ID пользователя", "Действие", "Детали"]
        ws_logs.append(headers_logs)
        
        for cell in ws_logs[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = header_alignment
        
        for log in logs:
            ws_logs.append([
                log['id'],
                log['timestamp'].strftime('%Y-%m-%d %H:%M:%S') if log['timestamp'] else '',
                log['user_name'] or 'Неизвестно',
                log['user_id'] or '',
                log['action'] or '',
                log['details'] or ''
            ])
        
        # Автоподбор ширины колонок для логов
        for column in ws_logs.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            adjusted_width = min(max_length + 2, 50)
            ws_logs.column_dimensions[column_letter].width = adjusted_width
        
        # 3. Лист с обратной связью
        ws_feedback = wb.create_sheet("Обратная связь")
        cur.execute("""
            SELECT f.id, f.feedback_type, f.title, f.message, f.status, f.priority,
                   f.page_url, f.admin_notes, f.created_at, f.updated_at,
                   u.display_name as user_name, u.email as user_email
            FROM feedback f
            LEFT JOIN users u ON f.user_id = u.id
            ORDER BY f.created_at DESC
        """)
        feedbacks = cur.fetchall()
        
        headers_feedback = ["ID", "Тип", "Заголовок", "Сообщение", "Статус", "Приоритет",
                           "URL страницы", "Заметки админа", "Дата создания", "Дата обновления",
                           "Пользователь", "Email пользователя"]
        ws_feedback.append(headers_feedback)
        
        for cell in ws_feedback[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = header_alignment
        
        for fb in feedbacks:
            ws_feedback.append([
                fb['id'],
                fb['feedback_type'] or '',
                fb['title'] or '',
                fb['message'] or '',
                fb['status'] or '',
                fb['priority'] or '',
                fb['page_url'] or '',
                fb['admin_notes'] or '',
                fb['created_at'].strftime('%Y-%m-%d %H:%M:%S') if fb['created_at'] else '',
                fb['updated_at'].strftime('%Y-%m-%d %H:%M:%S') if fb['updated_at'] else '',
                fb['user_name'] or 'Аноним',
                fb['user_email'] or ''
            ])
        
        # Автоподбор ширины колонок для обратной связи
        for column in ws_feedback.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            adjusted_width = min(max_length + 2, 50)
            ws_feedback.column_dimensions[column_letter].width = adjusted_width
        
        # 4. Лист с сессиями пользователей
        ws_sessions = wb.create_sheet("Сессии пользователей")
        cur.execute("""
            SELECT s.id, s.user_id, u.display_name as user_name, s.session_token,
                   s.ip_address, s.user_agent, s.created_at, s.expires_at, s.is_active
            FROM user_sessions s
            LEFT JOIN users u ON s.user_id = u.id
            ORDER BY s.created_at DESC
            LIMIT 10000
        """)
        sessions = cur.fetchall()
        
        headers_sessions = ["ID", "ID пользователя", "Пользователь", "Токен сессии",
                           "IP адрес", "User Agent", "Дата создания", "Дата истечения", "Активна"]
        ws_sessions.append(headers_sessions)
        
        for cell in ws_sessions[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = header_alignment
        
        for sess in sessions:
            ws_sessions.append([
                sess['id'],
                sess['user_id'] or '',
                sess['user_name'] or 'Неизвестно',
                sess['session_token'] or '',
                sess['ip_address'] or '',
                sess['user_agent'] or '',
                sess['created_at'].strftime('%Y-%m-%d %H:%M:%S') if sess['created_at'] else '',
                sess['expires_at'].strftime('%Y-%m-%d %H:%M:%S') if sess['expires_at'] else '',
                'Да' if sess['is_active'] else 'Нет'
            ])
        
        # Автоподбор ширины колонок для сессий
        for column in ws_sessions.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            adjusted_width = min(max_length + 2, 50)
            ws_sessions.column_dimensions[column_letter].width = adjusted_width
        
        cur.close()
        conn.close()
        
        # Сохраняем в память
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        
        # Логируем действие
        log_action(session['user_db_id'], "EXPORT_EXCEL", "Экспорт данных в Excel")
        
        # Генерируем имя файла с датой
        filename = f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=filename
        )
        
    except Exception as e:
        print(f"Ошибка экспорта в Excel: {e}")
        import traceback
        traceback.print_exc()
        flash(f"Ошибка при экспорте данных: {str(e)}", "error")
        return redirect("/admin")


@app.route("/admin/import/users", methods=["POST"])
@admin_required
def import_users_from_excel():
    """Импорт пользователей из Excel.
    
    Логика:
    - читаем лист \"Пользователи\" (или первый лист);
    - по каждой строке пытаемся создать нового пользователя;
    - существующие пользователи (по username/email/spotify_id) пропускаются;
    - ничего не перезаписываем, только добавляем новые записи.
    """
    if not OPENPYXL_AVAILABLE:
        flash(
            "Библиотека OpenPyXL не установлена. Установите её командой: pip install openpyxl",
            "error",
        )
        return redirect("/admin")

    file = request.files.get("users_file")
    if not file or file.filename == "":
        flash("Файл не выбран", "error")
        return redirect("/admin")

    filename = file.filename.lower()
    if not (filename.endswith(".xlsx") or filename.endswith(".xlsm")):
        flash("Поддерживаются только Excel файлы (.xlsx, .xlsm)", "error")
        return redirect("/admin")

    try:
        # Читаем книгу из потока (в память)
        file.stream.seek(0)
        wb = load_workbook(file.stream, data_only=True)

        # Пытаемся взять лист \"Пользователи\", иначе первый лист
        sheet = wb["Пользователи"] if "Пользователи" in wb.sheetnames else wb.active

        # Читаем заголовок и строим маппинг колонок
        header_row = None
        for row in sheet.iter_rows(min_row=1, max_row=1, values_only=True):
            header_row = row
            break

        if not header_row:
            flash("В Excel-файле нет заголовка (первая строка пуста)", "error")
            return redirect("/admin")

        header_map = {}
        for idx, val in enumerate(header_row):
            if not val:
                continue
            key = str(val).strip().lower()
            header_map[key] = idx

        # Ожидаемые названия колонок (из экспортного файла)
        col_username = header_map.get("логин")
        col_display_name = header_map.get("имя")
        col_email = header_map.get("email")
        col_spotify_id = header_map.get("spotify id")
        col_role = header_map.get("роль")
        col_blocked = header_map.get("заблокирован")

        if col_username is None and col_email is None and col_spotify_id is None:
            flash(
                "В Excel-файле должны быть хотя бы один из столбцов: 'Логин', 'Email' или 'Spotify ID'",
                "error",
            )
            return redirect("/admin")

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        inserted = 0
        skipped_existing = 0
        skipped_invalid = 0

        # Чтобы не создавать дубликаты в рамках одного файла
        seen_usernames = set()
        seen_emails = set()
        seen_spotify_ids = set()

        for row_idx, row in enumerate(
            sheet.iter_rows(min_row=2, values_only=True), start=2
        ):
            if not any(row):
                continue  # полностью пустая строка

            username = (
                str(row[col_username]).strip() if col_username is not None and row[col_username] else ""
            )
            display_name = (
                str(row[col_display_name]).strip()
                if col_display_name is not None and row[col_display_name]
                else ""
            )
            email = (
                str(row[col_email]).strip().lower()
                if col_email is not None and row[col_email]
                else ""
            )
            spotify_id = (
                str(row[col_spotify_id]).strip()
                if col_spotify_id is not None and row[col_spotify_id]
                else ""
            )
            role = (
                str(row[col_role]).strip().lower()
                if col_role is not None and row[col_role]
                else "user"
            )
            if role not in ("user", "admin"):
                role = "user"

            is_blocked = False
            if col_blocked is not None and row[col_blocked]:
                val = str(row[col_blocked]).strip().lower()
                is_blocked = val in ("да", "true", "1", "yes")

            # Должен быть хотя бы username или email или spotify_id
            if not username and not email and not spotify_id:
                skipped_invalid += 1
                continue

            # Локальные дубликаты в файле
            if username and username.lower() in seen_usernames:
                skipped_invalid += 1
                continue
            if email and email in seen_emails:
                skipped_invalid += 1
                continue
            if spotify_id and spotify_id in seen_spotify_ids:
                skipped_invalid += 1
                continue

            # Проверяем, существует ли уже такой пользователь в БД
            cur.execute(
                """
                SELECT id FROM users
                WHERE (%s IS NOT NULL AND username = %s)
                   OR (%s IS NOT NULL AND email = %s)
                   OR (%s IS NOT NULL AND spotify_id = %s)
                """,
                (
                    username if username else None,
                    username if username else None,
                    email if email else None,
                    email if email else None,
                    spotify_id if spotify_id else None,
                    spotify_id if spotify_id else None,
                ),
            )
            existing = cur.fetchone()
            if existing:
                skipped_existing += 1
                continue

            # Генерируем временный пароль
            raw_password = "".join(
                random.choices(string.ascii_letters + string.digits, k=12)
            )
            password_hash = generate_password_hash(raw_password)

            # Если нет username, но есть email или spotify_id — подставляем что-то разумное
            if not username:
                if email:
                    username = f"import_{email.split('@')[0]}"
                elif spotify_id:
                    username = f"import_spotify_{spotify_id}"
                else:
                    username = f"import_user_{random.randint(1000,9999)}"

            # Если нет display_name — берем username
            if not display_name:
                display_name = username

            try:
                cur.execute(
                    """
                    INSERT INTO users (username, password_hash, email, spotify_id, display_name, role, is_blocked, email_verified)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        username,
                        password_hash,
                        email if email else None,
                        spotify_id if spotify_id else None,
                        display_name,
                        role,
                        is_blocked,
                        False,
                    ),
                )
                new_user = cur.fetchone()
                conn.commit()

                inserted += 1
                seen_usernames.add(username.lower())
                if email:
                    seen_emails.add(email)
                if spotify_id:
                    seen_spotify_ids.add(spotify_id)

                # Логируем создание пользователя
                log_action(
                    session["user_db_id"],
                    "IMPORT_USER",
                    f"Импортирован пользователь из Excel: id={new_user['id']}, username={username}",
                )
            except UniqueViolation:
                conn.rollback()
                skipped_existing += 1
            except Exception as e:
                conn.rollback()
                print(f"Ошибка при импорте пользователя из строки {row_idx}: {e}")
                skipped_invalid += 1

        cur.close()
        conn.close()

        flash(
            f"Импорт завершен. Добавлено новых пользователей: {inserted}. "
            f"Пропущено существующих: {skipped_existing}. "
            f"Пропущено некорректных строк: {skipped_invalid}.",
            "success" if inserted > 0 else "info",
        )
        return redirect("/admin")
    except Exception as e:
        print(f"Ошибка импорта пользователей из Excel: {e}")
        import traceback
        traceback.print_exc()
        flash("Ошибка при импорте пользователей. Проверьте формат файла.", "error")
    return redirect("/admin")

@app.route("/logout")
def logout():
    if 'user_db_id' in session:
        session_token = session.get('_session_token')
        if session_token:
            deactivate_user_session(session_token)
        
        user_id = session.get('user_db_id')
        if user_id:
            log_action(user_id, "LOGOUT", "Выход из аккаунта")
    
    session.clear()
    return redirect("/")

if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)