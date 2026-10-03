FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home atlas
COPY src ./src
COPY data/fixtures ./data/fixtures
RUN mkdir -p /app/data/runtime && chown -R atlas:atlas /app
USER atlas
EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live',timeout=2)"
CMD ["python", "-m", "src"]

