# pg_dump/pg_restore must match the server major version (16), so the
# backup image is built on the same postgres:16 base as the database.
FROM postgres:16-alpine
# gosu exists only for the server entrypoint's root -> postgres step-down.
# This image never runs the server or starts as root, and the bundled gosu
# was built with Go 1.24.6 (CVE-2025-68121, CRITICAL; caught by CI Trivy).
# Remove the binary rather than suppress the finding.
RUN apk add --no-cache aws-cli && rm -f /usr/local/bin/gosu
COPY backup.sh restore.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/backup.sh /usr/local/bin/restore.sh
USER 70
ENTRYPOINT []
CMD ["backup.sh"]
