# Docker Deployment Guide

This application is ready to be deployed on a VPS using Docker and Docker Compose.

## Prerequisites

1.  **Docker** and **Docker Compose** installed on your VPS.
2.  A `.env` file with your credentials (use `.env.example` as a template).

## Deployment Steps

1.  **Upload the files** to your VPS. You only need the following:
    - `dashboard/` (the entire directory)
    - `Dockerfile`
    - `docker-compose.yml`
    - `requirements-prod.txt`
    - `.env`

2.  **Configure Environment Variables**:
    Create or update your `.env` file on the VPS:
    ```bash
    cp .env.example .env
    nano .env
    ```

3.  **Build and Start the Container**:
    Run the following command in the directory containing `docker-compose.yml`:
    ```bash
    docker compose up -d --build
    ```

4.  **Verify**:
    The application will be running on port `8081`. You can access it at `http://your-vps-ip:8081`.

## Managing the Container

- **View Logs**:
  ```bash
  docker compose logs -f
  ```
- **Stop the App**:
  ```bash
  docker compose down
  ```
- **Restart the App**:
  ```bash
  docker compose restart dashboard
  ```

## Performance & Scaling
The Docker setup uses **Gunicorn** with 4 workers and **Uvicorn** workers, making it suitable for production traffic.
