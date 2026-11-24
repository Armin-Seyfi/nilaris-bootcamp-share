\set ON_ERROR_STOP on
\getenv app_password A1_APP_PASSWORD
\getenv migration_password A1_MIGRATION_PASSWORD
\getenv test_password A1_TEST_PASSWORD
\getenv n8n_password A1_N8N_PASSWORD
CREATE ROLE nilaris_app LOGIN PASSWORD :'app_password' CONNECTION LIMIT 4;
CREATE ROLE nilaris_migration LOGIN PASSWORD :'migration_password' CONNECTION LIMIT 2;
CREATE ROLE nilaris_test LOGIN CREATEDB PASSWORD :'test_password' CONNECTION LIMIT 2;
CREATE ROLE nilaris_n8n LOGIN PASSWORD :'n8n_password' CONNECTION LIMIT 2;
CREATE DATABASE nilaris_demo OWNER nilaris_migration;
CREATE DATABASE nilaris_n8n OWNER nilaris_n8n;
REVOKE CONNECT ON DATABASE nilaris_demo FROM PUBLIC;
GRANT CONNECT ON DATABASE nilaris_demo TO nilaris_app;
REVOKE CONNECT ON DATABASE nilaris_n8n FROM PUBLIC;
\connect nilaris_demo
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO nilaris_app;
ALTER DEFAULT PRIVILEGES FOR ROLE nilaris_migration IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO nilaris_app;
ALTER DEFAULT PRIVILEGES FOR ROLE nilaris_migration IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO nilaris_app;
