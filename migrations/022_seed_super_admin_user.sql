
WITH upserted_user AS (
    INSERT INTO tbl_users (
        username,
        email,
        password
    )
    VALUES (
        'superadmin',
        'admin@example.com',
        'CHANGE_ME_HASHED_PASSWORD'
    )
    ON CONFLICT (email)
    DO UPDATE
    SET
        username = EXCLUDED.username
    RETURNING id
),

role_row AS (
    SELECT id AS role_id
    FROM tbl_roles
    WHERE name = 'super_admin'
),

link_role AS (
    INSERT INTO tbl_user_roles (user_id, role_id)
    SELECT upserted_user.id, role_row.role_id
    FROM upserted_user, role_row
    ON CONFLICT DO NOTHING
    RETURNING user_id, role_id
)

SELECT
    upserted_user.id AS seeded_user_id,
    role_row.role_id AS seeded_role_id
FROM upserted_user, role_row;
