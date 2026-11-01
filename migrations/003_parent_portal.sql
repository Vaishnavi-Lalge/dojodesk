CREATE TABLE parent_accounts (
    id            SERIAL PRIMARY KEY,
    username      TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    full_name     TEXT NOT NULL,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX parent_accounts_username_key ON parent_accounts (lower(username));

CREATE TABLE parent_students (
    parent_id  INTEGER NOT NULL REFERENCES parent_accounts (id) ON DELETE CASCADE,
    student_id INTEGER NOT NULL REFERENCES students (id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (parent_id, student_id)
);
CREATE INDEX parent_students_student_idx ON parent_students (student_id);
