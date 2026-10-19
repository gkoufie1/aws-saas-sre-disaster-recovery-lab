# pg_dump/pg_restore must match the server major version (16), so the
# backup image is built on the same postgres:16 base as the database.
FROM postgres:16-alpine
RUN apk add --no-cache aws-cli
COPY backup.sh restore.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/backup.sh /usr/local/bin/restore.sh
USER 70
ENTRYPOINT []
CMD ["backup.sh"]
