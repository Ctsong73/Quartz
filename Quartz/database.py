from sqlalchemy import create_engine, text
import os

class SQL:
    def __init__(self, database_url=None):
        # Get database URL from parameter or environment (Supabase in production, SQLite locally)
        self.database_url = database_url or os.environ.get('DATABASE_URL', 'sqlite:///finance.db')
        
        # Fix Supabase URL format (Render gives 'postgres://', but SQLAlchemy requires 'postgresql://')
        if self.database_url.startswith('postgres://'):
            self.database_url = self.database_url.replace('postgres://', 'postgresql://', 1)
        
        # Check database engine type
        if self.database_url.startswith('sqlite://'):
            self.db_type = 'sqlite'
            # Enable multi-thread access for SQLite in Flask
            self.engine = create_engine(
                self.database_url,
                connect_args={'check_same_thread': False}
            )
            print(f"[SQLAlchemy] Running with SQLite database engine: {self.database_url}")
        else:
            self.db_type = 'postgresql'
            # Connection pooling optimized for Render + Supabase PostgreSQL
            self.engine = create_engine(
                self.database_url,
                pool_size=5,
                max_overflow=10,
                pool_pre_ping=True,
                pool_recycle=3600
            )
            print(f"[SQLAlchemy] Connected to PostgreSQL (Supabase) with connection pool.")
            self._init_db()

    def _init_db(self):
        """Automatically initialize tables on Supabase if they do not exist."""
        try:
            with self.engine.begin() as conn:
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        username VARCHAR(255) UNIQUE NOT NULL,
                        hash VARCHAR(255) NOT NULL,
                        cash NUMERIC NOT NULL DEFAULT 1000000.00,
                        recovery_answer VARCHAR(255)
                    );
                    CREATE TABLE IF NOT EXISTS portfolio (
                        user_id INTEGER NOT NULL,
                        symbol VARCHAR(50) NOT NULL,
                        shares INTEGER NOT NULL,
                        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS transactions (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        symbol VARCHAR(50) NOT NULL,
                        shares INTEGER NOT NULL,
                        price DOUBLE PRECISION NOT NULL,
                        type VARCHAR(20) NOT NULL,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS ai_analysis (
                        symbol VARCHAR(50) PRIMARY KEY,
                        analysis TEXT,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE IF NOT EXISTS account_history (
                        user_id INTEGER NOT NULL,
                        date DATE NOT NULL,
                        nlv NUMERIC NOT NULL,
                        PRIMARY KEY (user_id, date),
                        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                    );
                """))
            print("[SQLAlchemy] PostgreSQL database schema verified/initialized successfully.")
        except Exception as e:
            print(f"[SQLAlchemy] Warning: Could not auto-initialize tables: {e}")

    def execute(self, query, *args):
        """Executes SQL query using SQLAlchemy engine while maintaining SQLite-style helper API."""
        if len(args) == 1 and isinstance(args[0], (list, tuple)):
            args = args[0]

        formatted_query = query
        if self.db_type == 'postgresql':
            # Translate SQLite date functions to PostgreSQL standard
            if "DATE('now', 'localtime')" in formatted_query:
                formatted_query = formatted_query.replace("DATE('now', 'localtime')", "CURRENT_DATE")
            if "DATE('now')" in formatted_query:
                formatted_query = formatted_query.replace("DATE('now')", "CURRENT_DATE")

        # Convert positional ? placeholders to named :p0, :p1 parameters for SQLAlchemy text()
        parts = formatted_query.split('?')
        params = {}
        sql_with_params = ""
        for i, part in enumerate(parts[:-1]):
            sql_with_params += part + f":p{i}"
            params[f"p{i}"] = args[i]
        sql_with_params += parts[-1]

        with self.engine.begin() as conn:
            result = conn.execute(text(sql_with_params), params)
            stmt_clean = query.strip().upper()
            if stmt_clean.startswith("SELECT"):
                rows = result.fetchall()
                return [dict(row._mapping) for row in rows]
            elif stmt_clean.startswith("INSERT"):
                return getattr(result, "lastrowid", None) or result.rowcount
            else:
                return result.rowcount

# Create global database instance
db = SQL()

