FastCommand
FastCommand is a simple and fast framework for building APIs using FastAPI. It organizes backend logic into clear, reusable commands so developers can focus on business features and write less boilerplate code.

Features
Command-driven architecture for clean code
Support for multiple authentication roles (admin, user, API tokens)
Built-in easy testing tools to encourage quality code
Define database schemas and migrations directly in Python
Flexible job queue system: in-memory for development, database or RabbitMQ for production
Cron-style task scheduling for automated jobs
Developer-friendly CLI commands for managing database and app state


docker compose up -d --build
docker compose up -d --scale generate-fe-consumer=2 generate-fe-consumer