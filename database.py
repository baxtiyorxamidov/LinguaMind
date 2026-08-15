import os
import psycopg

from dotenv import load_dotenv
from psycopg.rows import dict_row

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
def get_db():
    conn = psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row
    )
    return conn

def create_tables():

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (

        id SERIAL PRIMARY KEY,

        full_name TEXT NOT NULL,

        email TEXT UNIQUE NOT NULL,

        password TEXT NOT NULL

    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS vocabulary (

        id SERIAL PRIMARY KEY,

        user_id INTEGER NOT NULL,

        english_word TEXT NOT NULL,

        uzbek_word TEXT NOT NULL,

        example_sentence TEXT,

        status TEXT DEFAULT 'New',

        favorite INTEGER DEFAULT 0,

        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

        FOREIGN KEY(user_id) REFERENCES users(id)

    )
    """)

    conn.commit()
    conn.close()


if __name__ == "__main__":
    create_tables()
    print("Database created successfully!")