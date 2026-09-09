# The only route out of the dependency-install sandbox.
#
# Built here rather than pulled from a third-party proxy image: this container
# is the egress policy, and what it is made of should be as reviewable as the
# policy itself. Alpine's squid, pinned, with docker/squid.conf and nothing else.
FROM alpine:3.21

RUN apk add --no-cache squid=6.12-r0 \
 # Squid refuses to start without these, and the base image does not create
 # them with the ownership it needs.
 && mkdir -p /var/cache/squid /var/log/squid /var/run/squid \
 && chown -R squid:squid /var/cache/squid /var/log/squid /var/run/squid

COPY docker/squid.conf /etc/squid/squid.conf

# 🔴 Checked at BUILD time. A typo in an ACL is a config squid rejects at
# startup, and a proxy that will not start reads downstream as "dependency
# install is broken" from inside a container nobody is watching. `-k parse`
# fails the image build instead.
RUN squid -k parse -f /etc/squid/squid.conf

USER squid

# -N foreground, -d1 so denials reach `docker logs`. No cache directories to
# initialise because the config caches nothing.
ENTRYPOINT ["squid", "-N", "-d1", "-f", "/etc/squid/squid.conf"]
