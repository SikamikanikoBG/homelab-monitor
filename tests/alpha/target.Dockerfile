# A throw-away "box" for the agent's alpha test: sshd + its own Docker daemon
# (docker-in-docker). Nothing from the real host is mounted into it.
FROM docker:27-dind
RUN apk add --no-cache --upgrade openssh-client openssh-server sudo bash python3 shadow \
 && ssh-keygen -A \
 && adduser -D -s /bin/bash lab \
 && echo "lab:$(head -c 24 /dev/urandom | base64)" | chpasswd \
 && (addgroup -S docker 2>/dev/null || true) && addgroup lab docker \
 && echo 'lab ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/lab \
 && sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
COPY target-entry.sh /usr/local/bin/target-entry.sh
RUN chmod +x /usr/local/bin/target-entry.sh
ENTRYPOINT ["/usr/local/bin/target-entry.sh"]
