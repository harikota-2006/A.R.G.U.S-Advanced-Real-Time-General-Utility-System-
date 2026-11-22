# Public service uses a separate image and never copies personal configuration or daemon code.
FROM python:3.12-slim
RUN useradd --create-home --uid 10001 argus
WORKDIR /app
COPY requirements-termux.txt requirements-windows.txt ./
RUN pip install --no-cache-dir -r requirements-termux.txt -r requirements-windows.txt
COPY *.py ./
COPY public public
COPY static/visualizer.js static/visualizer.js
COPY static/pcm-worklet.js static/pcm-worklet.js
COPY static/style.css static/style.css
COPY static/vendor/three.min.js static/vendor/three.min.js
RUN mkdir /data && chown argus:argus /data
USER argus
ENV ARGUS_PUBLIC_DEPLOYMENT=1 PUBLIC_DATA_DIR=/data
EXPOSE 8080
CMD ["uvicorn","public.app:app","--host","0.0.0.0","--port","8080","--workers","1","--ws-max-size","900000","--no-proxy-headers"]
