FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
RUN pip install httpx==0.28.1 prometheus-client==0.21.1
COPY loadgen.py .
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001
EXPOSE 9100
CMD ["python", "loadgen.py"]
