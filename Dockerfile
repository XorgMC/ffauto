FROM alpine:latest

RUN apk add py3-pip py3-psutil ffmpeg shadow
RUN apk add gosu --repository https://dl-cdn.alpinelinux.org/alpine/edge/testing
#RUN echo '@edge https://dl-cdn.alpinelinux.org/alpine/edge/testing' >> /etc/apk/repositories &&\
#  apk add --no-cache py3-pip py3-psutil ffmpeg gosu shadow

WORKDIR /app
COPY requirements.txt requirements.txt
RUN pip3 install -r requirements.txt

COPY entrypoint.sh /entrypoint.sh
RUN chmod a+x /entrypoint.sh

COPY . .

ENV FFA_ENV=1
ENTRYPOINT ["/entrypoint.sh"]
CMD [ "python3", "main.py"]
