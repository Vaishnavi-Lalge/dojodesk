-- DojoDesk schema. Applied once by `flask --app app db-upgrade` (tracked in schema_migrations).
-- Timezone note: app_today() fixes "today" to the dojo's timezone. Change 'Asia/Kolkata' below
-- BEFORE the first run if your dojo is elsewhere (and set TIMEZONE in the environment).

CREATE FUNCTION app_today() RETURNS date LANGUAGE sql STABLE AS
$$ SELECT (now() AT TIME ZONE 'Asia/Kolkata')::date $$;

CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS
$$ BEGIN NEW.updated_at = now(); RETURN NEW; END $$;

CREATE SEQUENCE student_code_seq START 1;
CREATE SEQUENCE batch_code_seq START 1;
CREATE SEQUENCE receipt_seq START 1;

CREATE TABLE admins (
    id            SERIAL PRIMARY KEY,
    username      TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    full_name     TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'instructor' CHECK (role IN ('admin', 'instructor')),
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX admins_username_key ON admins (lower(username));

CREATE TABLE belt_ranks (
    id                 SERIAL PRIMARY KEY,
    name               TEXT NOT NULL UNIQUE,
    rank_order         INTEGER NOT NULL UNIQUE CHECK (rank_order > 0),
    color_hex          TEXT NOT NULL DEFAULT '#CBD5E1' CHECK (color_hex ~ '^#[0-9A-Fa-f]{6}$'),
    requirements       TEXT,
    min_months         INTEGER NOT NULL DEFAULT 0 CHECK (min_months >= 0),
    min_attendance_pct NUMERIC(5,2) NOT NULL DEFAULT 0 CHECK (min_attendance_pct BETWEEN 0 AND 100),
    is_active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE students (
    id              SERIAL PRIMARY KEY,
    student_code    TEXT NOT NULL UNIQUE,
    full_name       TEXT NOT NULL CHECK (length(trim(full_name)) > 0),
    photo           BYTEA,
    photo_mime      TEXT,
    dob             DATE NOT NULL,
    gender          TEXT CHECK (gender IN ('male', 'female', 'other')),
    phone           TEXT NOT NULL,
    guardian_name   TEXT,
    guardian_phone  TEXT,
    email           TEXT,
    address         TEXT,
    joining_date    DATE NOT NULL,
    current_belt_id INTEGER NOT NULL REFERENCES belt_ranks (id),
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'archived')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX students_status_idx ON students (status);
CREATE INDEX students_name_idx ON students (lower(full_name));
CREATE INDEX students_belt_idx ON students (current_belt_id);
CREATE INDEX students_joining_idx ON students (joining_date);
CREATE TRIGGER students_updated BEFORE UPDATE ON students FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE batches (
    id            SERIAL PRIMARY KEY,
    batch_code    TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL UNIQUE,
    instructor    TEXT NOT NULL,
    training_days TEXT[] NOT NULL DEFAULT '{}',
    start_time    TIME NOT NULL,
    end_time      TIME NOT NULL,
    capacity      INTEGER NOT NULL CHECK (capacity > 0),
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (end_time > start_time)
);
CREATE TRIGGER batches_updated BEFORE UPDATE ON batches FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE batch_enrollments (
    id          SERIAL PRIMARY KEY,
    student_id  INTEGER NOT NULL REFERENCES students (id),
    batch_id    INTEGER NOT NULL REFERENCES batches (id),
    enrolled_on DATE NOT NULL,
    ended_on    DATE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (ended_on IS NULL OR ended_on >= enrolled_on)
);
CREATE UNIQUE INDEX batch_enrollments_active_key ON batch_enrollments (student_id, batch_id) WHERE ended_on IS NULL;
CREATE INDEX batch_enrollments_batch_idx ON batch_enrollments (batch_id) WHERE ended_on IS NULL;

CREATE TABLE attendance (
    id           SERIAL PRIMARY KEY,
    student_id   INTEGER NOT NULL REFERENCES students (id),
    batch_id     INTEGER NOT NULL REFERENCES batches (id),
    session_date DATE NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('present', 'absent', 'late', 'excused')),
    remarks      TEXT,
    marked_by    INTEGER REFERENCES admins (id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (student_id, batch_id, session_date)
);
CREATE INDEX attendance_date_idx ON attendance (session_date);
CREATE INDEX attendance_batch_date_idx ON attendance (batch_id, session_date);
CREATE TRIGGER attendance_updated BEFORE UPDATE ON attendance FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE fee_plans (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    amount     NUMERIC(10,2) NOT NULL CHECK (amount > 0),
    period     TEXT NOT NULL CHECK (period IN ('monthly', 'term')),
    is_active  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE student_fees (
    id           SERIAL PRIMARY KEY,
    student_id   INTEGER NOT NULL REFERENCES students (id),
    plan_id      INTEGER REFERENCES fee_plans (id),
    description  TEXT NOT NULL,
    period_label TEXT NOT NULL,
    due_date     DATE NOT NULL,
    amount_due   NUMERIC(10,2) NOT NULL CHECK (amount_due > 0),
    created_by   INTEGER REFERENCES admins (id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- one tuition fee per student, plan and period
CREATE UNIQUE INDEX student_fees_plan_period_key ON student_fees (student_id, plan_id, period_label) WHERE plan_id IS NOT NULL;
CREATE INDEX student_fees_student_idx ON student_fees (student_id);
CREATE INDEX student_fees_due_idx ON student_fees (due_date);

CREATE TABLE payments (
    id                  SERIAL PRIMARY KEY,
    receipt_no          TEXT NOT NULL UNIQUE,
    fee_id              INTEGER NOT NULL REFERENCES student_fees (id),
    kind                TEXT NOT NULL DEFAULT 'payment' CHECK (kind IN ('payment', 'refund')),
    amount              NUMERIC(10,2) NOT NULL,   -- refunds are stored as negative amounts
    method              TEXT NOT NULL CHECK (method IN ('cash', 'upi', 'card', 'bank_transfer', 'cheque', 'other')),
    paid_on             DATE NOT NULL,
    reference           TEXT,
    reason              TEXT,
    original_payment_id INTEGER REFERENCES payments (id),
    idempotency_key     TEXT UNIQUE,              -- blocks double-submitted payment forms
    recorded_by         INTEGER REFERENCES admins (id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((kind = 'payment' AND amount > 0)
        OR (kind = 'refund' AND amount < 0 AND reason IS NOT NULL AND original_payment_id IS NOT NULL))
);
CREATE INDEX payments_fee_idx ON payments (fee_id);
CREATE INDEX payments_paid_on_idx ON payments (paid_on);

-- The payment ledger is append-only: corrections are made with refund rows, never edits.
CREATE FUNCTION payments_immutable() RETURNS trigger LANGUAGE plpgsql AS
$$ BEGIN RAISE EXCEPTION 'payments are append-only; record a refund/correction instead'; END $$;
CREATE TRIGGER payments_no_change BEFORE UPDATE OR DELETE ON payments FOR EACH ROW EXECUTE FUNCTION payments_immutable();

CREATE VIEW v_student_fees AS
SELECT f.*,
       COALESCE(p.paid, 0)                         AS amount_paid,
       f.amount_due - COALESCE(p.paid, 0)          AS balance,
       CASE WHEN f.amount_due - COALESCE(p.paid, 0) <= 0 THEN 'paid'
            WHEN f.due_date < app_today()          THEN 'overdue'
            WHEN COALESCE(p.paid, 0) > 0           THEN 'partial'
            ELSE 'pending' END                     AS status
FROM student_fees f
LEFT JOIN (SELECT fee_id, SUM(amount) AS paid FROM payments GROUP BY fee_id) p ON p.fee_id = f.id;

CREATE TABLE examinations (
    id                SERIAL PRIMARY KEY,
    student_id        INTEGER NOT NULL REFERENCES students (id),
    exam_date         DATE NOT NULL,
    from_belt_id      INTEGER NOT NULL REFERENCES belt_ranks (id),
    target_belt_id    INTEGER NOT NULL REFERENCES belt_ranks (id),
    eligible          BOOLEAN NOT NULL DEFAULT FALSE,
    eligibility_notes TEXT,
    exam_fee          NUMERIC(10,2) NOT NULL DEFAULT 0 CHECK (exam_fee >= 0),
    fee_id            INTEGER REFERENCES student_fees (id),
    result            TEXT NOT NULL DEFAULT 'pending' CHECK (result IN ('pending', 'passed', 'failed')),
    score             NUMERIC(5,2) CHECK (score IS NULL OR score BETWEEN 0 AND 100),
    remarks           TEXT,
    created_by        INTEGER REFERENCES admins (id),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX examinations_one_pending_key ON examinations (student_id) WHERE result = 'pending';
CREATE INDEX examinations_date_idx ON examinations (exam_date);
CREATE TRIGGER examinations_updated BEFORE UPDATE ON examinations FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE belt_promotions (
    id           SERIAL PRIMARY KEY,
    student_id   INTEGER NOT NULL REFERENCES students (id),
    exam_id      INTEGER NOT NULL UNIQUE REFERENCES examinations (id),
    from_belt_id INTEGER NOT NULL REFERENCES belt_ranks (id),
    to_belt_id   INTEGER NOT NULL REFERENCES belt_ranks (id),
    promoted_on  DATE NOT NULL,
    remarks      TEXT,
    approved_by  INTEGER NOT NULL REFERENCES admins (id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX belt_promotions_student_idx ON belt_promotions (student_id);
CREATE INDEX belt_promotions_date_idx ON belt_promotions (promoted_on);

CREATE TABLE audit_logs (
    id         BIGSERIAL PRIMARY KEY,
    admin_id   INTEGER REFERENCES admins (id),
    action     TEXT NOT NULL,
    entity     TEXT NOT NULL,
    entity_id  TEXT,
    details    JSONB NOT NULL DEFAULT '{}',
    ip_address TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX audit_logs_created_idx ON audit_logs (created_at DESC);
CREATE INDEX audit_logs_entity_idx ON audit_logs (entity, entity_id);
