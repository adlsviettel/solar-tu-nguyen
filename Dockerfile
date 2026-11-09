FROM python:3.10-slim
WORKDIR /app
COPY . /app
EXPOSE 8088
ENV PORT=8088
CMD ["python", "app.py"]
