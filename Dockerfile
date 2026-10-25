FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY ragnarok ./ragnarok
RUN pip install --no-cache-dir ".[fast]"
ENV RAGNAROK_HOME=/data OLLAMA_URL=http://host.docker.internal:11434
VOLUME /data
EXPOSE 8765
# bind UI to localhost inside the container; publish with -p 127.0.0.1:8765:8765 only
CMD ["ragnarok", "daemon", "--ui"]
