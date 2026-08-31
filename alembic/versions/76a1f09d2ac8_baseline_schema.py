"""baseline_schema — 初始表结构基线（13 张表 + 扩展 + 索引）

Revision ID: 76a1f09d2ac8
Revises:
Create Date: 2026-08-02 19:48:53.351942

说明：
- 项目使用原生 asyncpg SQL（无 SQLAlchemy ORM 模型），故基线迁移用 op.execute
  忠实还原线上库 DDL（pg_dump --schema-only 导出版本）。
- 已存在的线上库执行 `alembic stamp head` 标记基线；新库执行 `alembic upgrade head`。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '76a1f09d2ac8'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ---------- 扩展 ----------
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ---------- 1. conversation_memories ----------
    op.execute("""
        CREATE TABLE IF NOT EXISTS conversation_memories (
            id integer NOT NULL,
            user_id text NOT NULL,
            conversation_id text NOT NULL,
            role text NOT NULL,
            content text NOT NULL,
            created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id)
        )
    """)
    op.execute("CREATE SEQUENCE IF NOT EXISTS conversation_memories_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1")
    op.execute("ALTER SEQUENCE conversation_memories_id_seq OWNED BY conversation_memories.id")
    op.execute("ALTER TABLE conversation_memories ALTER COLUMN id SET DEFAULT nextval('conversation_memories_id_seq'::regclass)")

    # ---------- 2. invoices ----------
    op.execute("""
        CREATE TABLE IF NOT EXISTS invoices (
            id integer NOT NULL,
            invoice_no text NOT NULL,
            user_id text NOT NULL,
            order_nos text[] NOT NULL,
            total_amount numeric(14,2) NOT NULL,
            invoice_type text DEFAULT 'personal'::text NOT NULL,
            company_name text DEFAULT ''::text,
            company_tax_id text DEFAULT ''::text,
            status text DEFAULT 'pending'::text NOT NULL,
            pdf_url text DEFAULT ''::text,
            issued_at timestamp without time zone,
            created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            CONSTRAINT invoices_invoice_no_key UNIQUE (invoice_no)
        )
    """)
    op.execute("CREATE SEQUENCE IF NOT EXISTS invoices_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1")
    op.execute("ALTER SEQUENCE invoices_id_seq OWNED BY invoices.id")
    op.execute("ALTER TABLE invoices ALTER COLUMN id SET DEFAULT nextval('invoices_id_seq'::regclass)")

    # ---------- 3. knowledge_chunks ----------
    op.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            id integer NOT NULL,
            chunk_key text NOT NULL,
            source text DEFAULT 'travel'::text NOT NULL,
            heading text DEFAULT ''::text NOT NULL,
            content text NOT NULL,
            embedding public.vector(768),
            created_at timestamp without time zone DEFAULT now(),
            PRIMARY KEY (id),
            CONSTRAINT knowledge_chunks_chunk_key_key UNIQUE (chunk_key)
        )
    """)
    op.execute("CREATE SEQUENCE IF NOT EXISTS knowledge_chunks_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1")
    op.execute("ALTER SEQUENCE knowledge_chunks_id_seq OWNED BY knowledge_chunks.id")
    op.execute("ALTER TABLE knowledge_chunks ALTER COLUMN id SET DEFAULT nextval('knowledge_chunks_id_seq'::regclass)")

    # ---------- 4. payment_channels ----------
    op.execute("""
        CREATE TABLE IF NOT EXISTS payment_channels (
            id integer NOT NULL,
            channel_code text NOT NULL,
            channel_name text NOT NULL,
            icon text DEFAULT ''::text,
            is_active boolean DEFAULT true,
            fee_rate numeric(5,4) DEFAULT 0.0000,
            min_amount numeric(14,2) DEFAULT 0.01,
            max_amount numeric(14,2) DEFAULT 999999.00,
            sort_order integer DEFAULT 0,
            config jsonb DEFAULT '{}'::jsonb,
            created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
            updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            CONSTRAINT payment_channels_channel_code_key UNIQUE (channel_code)
        )
    """)
    op.execute("CREATE SEQUENCE IF NOT EXISTS payment_channels_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1")
    op.execute("ALTER SEQUENCE payment_channels_id_seq OWNED BY payment_channels.id")
    op.execute("ALTER TABLE payment_channels ALTER COLUMN id SET DEFAULT nextval('payment_channels_id_seq'::regclass)")

    # ---------- 5. payment_orders ----------
    op.execute("""
        CREATE TABLE IF NOT EXISTS payment_orders (
            order_no text NOT NULL,
            user_id text NOT NULL,
            order_type text NOT NULL,
            amount numeric(14,2) NOT NULL,
            fee numeric(14,2) DEFAULT 0.00 NOT NULL,
            status text DEFAULT 'pending'::text NOT NULL,
            subject text DEFAULT ''::text NOT NULL,
            body text DEFAULT ''::text,
            payment_method text DEFAULT 'balance'::text NOT NULL,
            channel_order_no text DEFAULT ''::text,
            paid_at timestamp without time zone,
            refunded_at timestamp without time zone,
            expire_at timestamp without time zone,
            notify_url text DEFAULT ''::text,
            callback_status text DEFAULT 'pending'::text,
            callback_times integer DEFAULT 0,
            metadata jsonb DEFAULT '{}'::jsonb,
            created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
            updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (order_no)
        )
    """)

    # ---------- 6. reconciliation_records ----------
    op.execute("""
        CREATE TABLE IF NOT EXISTS reconciliation_records (
            id integer NOT NULL,
            reconcile_date text NOT NULL,
            total_orders integer DEFAULT 0,
            total_amount numeric(14,2) DEFAULT 0.00,
            success_orders integer DEFAULT 0,
            success_amount numeric(14,2) DEFAULT 0.00,
            failed_orders integer DEFAULT 0,
            refund_orders integer DEFAULT 0,
            refund_amount numeric(14,2) DEFAULT 0.00,
            status text DEFAULT 'pending'::text,
            detail jsonb DEFAULT '{}'::jsonb,
            created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
            updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            CONSTRAINT reconciliation_records_reconcile_date_key UNIQUE (reconcile_date)
        )
    """)
    op.execute("CREATE SEQUENCE IF NOT EXISTS reconciliation_records_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1")
    op.execute("ALTER SEQUENCE reconciliation_records_id_seq OWNED BY reconciliation_records.id")
    op.execute("ALTER TABLE reconciliation_records ALTER COLUMN id SET DEFAULT nextval('reconciliation_records_id_seq'::regclass)")

    # ---------- 7. requests ----------
    op.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id text NOT NULL,
            start_time real,
            first_token_time real,
            end_time real,
            model_used text,
            status text,
            error text,
            input_tokens integer,
            PRIMARY KEY (id)
        )
    """)

    # ---------- 8. semantic_cache ----------
    op.execute("""
        CREATE TABLE IF NOT EXISTS semantic_cache (
            id bigint NOT NULL,
            query_hash character(64),
            query_text text,
            response text NOT NULL,
            hit_count integer DEFAULT 0,
            created_at timestamp without time zone DEFAULT now(),
            PRIMARY KEY (id),
            CONSTRAINT semantic_cache_query_hash_key UNIQUE (query_hash)
        )
    """)
    op.execute("CREATE SEQUENCE IF NOT EXISTS semantic_cache_id_seq START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1")
    op.execute("ALTER SEQUENCE semantic_cache_id_seq OWNED BY semantic_cache.id")
    op.execute("ALTER TABLE semantic_cache ALTER COLUMN id SET DEFAULT nextval('semantic_cache_id_seq'::regclass)")

    # ---------- 9. transaction_logs ----------
    op.execute("""
        CREATE TABLE IF NOT EXISTS transaction_logs (
            id bigint NOT NULL,
            order_no text NOT NULL,
            user_id text NOT NULL,
            tx_type text NOT NULL,
            amount numeric(14,2) NOT NULL,
            before_balance numeric(14,2) NOT NULL,
            after_balance numeric(14,2) NOT NULL,
            status text DEFAULT 'success'::text NOT NULL,
            remark text DEFAULT ''::text,
            operator text DEFAULT 'system'::text NOT NULL,
            created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id)
        )
    """)
    op.execute("CREATE SEQUENCE IF NOT EXISTS transaction_logs_id_seq START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1")
    op.execute("ALTER SEQUENCE transaction_logs_id_seq OWNED BY transaction_logs.id")
    op.execute("ALTER TABLE transaction_logs ALTER COLUMN id SET DEFAULT nextval('transaction_logs_id_seq'::regclass)")

    # ---------- 10. user_profiles ----------
    op.execute("""
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id text NOT NULL,
            profile jsonb DEFAULT '{}'::jsonb,
            updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id)
        )
    """)

    # ---------- 11. user_usage ----------
    op.execute("""
        CREATE TABLE IF NOT EXISTS user_usage (
            username text NOT NULL,
            date text NOT NULL,
            request_count integer DEFAULT 0,
            token_sum integer DEFAULT 0,
            PRIMARY KEY (username, date)
        )
    """)

    # ---------- 12. user_wallets ----------
    op.execute("""
        CREATE TABLE IF NOT EXISTS user_wallets (
            user_id text NOT NULL,
            balance numeric(14,2) DEFAULT 0.00 NOT NULL,
            frozen_amount numeric(14,2) DEFAULT 0.00 NOT NULL,
            total_recharged numeric(14,2) DEFAULT 0.00 NOT NULL,
            total_spent numeric(14,2) DEFAULT 0.00 NOT NULL,
            total_refunded numeric(14,2) DEFAULT 0.00 NOT NULL,
            status text DEFAULT 'active'::text NOT NULL,
            version integer DEFAULT 0 NOT NULL,
            created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
            updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id)
        )
    """)

    # ---------- 13. users ----------
    op.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username text NOT NULL,
            hashed_password text NOT NULL,
            created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
            role text DEFAULT 'user'::text,
            PRIMARY KEY (username)
        )
    """)

    # ---------- 索引 ----------
    op.execute("CREATE INDEX IF NOT EXISTS idx_conv_memories_compress ON conversation_memories USING btree (user_id, conversation_id, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_conv_memories_created ON conversation_memories USING btree (created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_conv_memories_lookup ON conversation_memories USING btree (user_id, conversation_id, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_inv_user ON invoices USING btree (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_kb_trgm ON knowledge_chunks USING gin (content public.gin_trgm_ops)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_embedding ON knowledge_chunks USING ivfflat (embedding public.vector_cosine_ops) WITH (lists='100')")
    op.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_embedding_project ON knowledge_chunks USING ivfflat (embedding public.vector_cosine_ops) WITH (lists='10')")
    op.execute("CREATE INDEX IF NOT EXISTS idx_po_created ON payment_orders USING btree (created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_po_status ON payment_orders USING btree (status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_po_user ON payment_orders USING btree (user_id, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_project_knowledge_embedding ON knowledge_chunks USING ivfflat (embedding public.vector_cosine_ops) WITH (lists='10')")
    op.execute("CREATE INDEX IF NOT EXISTS idx_semantic_cache_created ON semantic_cache USING btree (created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_semantic_trgm ON semantic_cache USING gin (query_text public.gin_trgm_ops)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_tx_created ON transaction_logs USING btree (created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_tx_order ON transaction_logs USING btree (order_no)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_tx_user ON transaction_logs USING btree (user_id, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_user_profiles_updated ON user_profiles USING btree (updated_at)")


def downgrade() -> None:
    """Downgrade schema — 按依赖逆序删除全部对象。"""
    op.execute("DROP TABLE IF EXISTS users CASCADE")
    op.execute("DROP TABLE IF EXISTS user_wallets CASCADE")
    op.execute("DROP TABLE IF EXISTS user_usage CASCADE")
    op.execute("DROP TABLE IF EXISTS user_profiles CASCADE")
    op.execute("DROP TABLE IF EXISTS transaction_logs CASCADE")
    op.execute("DROP TABLE IF EXISTS semantic_cache CASCADE")
    op.execute("DROP TABLE IF EXISTS requests CASCADE")
    op.execute("DROP TABLE IF EXISTS reconciliation_records CASCADE")
    op.execute("DROP TABLE IF EXISTS payment_orders CASCADE")
    op.execute("DROP TABLE IF EXISTS payment_channels CASCADE")
    op.execute("DROP TABLE IF EXISTS knowledge_chunks CASCADE")
    op.execute("DROP TABLE IF EXISTS invoices CASCADE")
    op.execute("DROP TABLE IF EXISTS conversation_memories CASCADE")
