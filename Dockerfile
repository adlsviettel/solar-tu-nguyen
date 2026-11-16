FROM python:3.10-slim
ENV TZ=Asia/Ho_Chi_Minh
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY . /app
EXPOSE 8088
ENV PORT=8088
CMD ["python", "app.py"]
