#!/usr/bin/env bash
#
# Brings up two independent phpIPAM instances and seeds them so
# phpipam-sync can be verified against real software.
#
# Idempotent enough to re-run: it tears the stack down first, so every
# run starts from nothing.
#
#   ./setup.sh          # bring up + seed
#   docker compose down # destroy everything (no volumes are declared)
#
# Afterwards:
#   instance 1 (source)  https://127.0.0.1:8443   app 'sync', READ-ONLY
#   instance 2 (replica) https://127.0.0.1:8444   app 'sync', read-write
#
# Instance 1's API app is deliberately read-only: the exporter is
# supposed to never write, and this makes that structurally enforced
# rather than merely claimed.

set -euo pipefail
cd "$(dirname "$0")"

SRC_TOKEN=SRCTOKEN0000000000000000000000
DST_TOKEN=DSTTOKEN0000000000000000000000

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "tearing down any previous lab"
docker compose down -v --remove-orphans >/dev/null 2>&1 || true

say "generating a throwaway self-signed cert"
mkdir -p certs nginx
openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout certs/server.key -out certs/server.crt -days 2 \
    -subj "/CN=ipam-lab" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" 2>/dev/null

for n in 1 2 3; do
cat > "nginx/ipam${n}.conf" <<EOF
server {
    listen 443 ssl;
    server_name _;
    ssl_certificate     /certs/server.crt;
    ssl_certificate_key /certs/server.key;
    client_max_body_size 16m;
    location / {
        proxy_pass http://ipam${n}:80;
        proxy_set_header Host              \$host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For   \$remote_addr;
        proxy_set_header X-Real-IP         \$remote_addr;
    }
}
EOF
done

say "starting containers"
docker compose up -d

say "waiting for the web containers to serve"
for _ in $(seq 1 40); do
    if docker compose exec -T ipam1 test -f /phpipam/db/SCHEMA.sql 2>/dev/null &&
       docker compose exec -T ipam2 test -f /phpipam/db/SCHEMA.sql 2>/dev/null &&
       docker compose exec -T ipam3 test -f /phpipam/db/SCHEMA.sql 2>/dev/null; then
        break
    fi
    sleep 3
done

say "loading the phpIPAM schema into both databases"
# The image ships the schema but does not load it; the web installer
# would otherwise have to be driven by hand. Note SCHEMA.sql also
# inserts demo sections/subnets, which the seed step below clears.
docker compose exec -T ipam1 cat /phpipam/db/SCHEMA.sql > /tmp/phpipam-schema.sql
for n in 1 2 3; do
    docker compose exec -T "db${n}" mariadb -uroot -prootpw phpipam < /tmp/phpipam-schema.sql
done
rm -f /tmp/phpipam-schema.sql

say "clearing demo data and enabling the API"
for n in 1 2 3; do
docker compose exec -T "db${n}" mariadb -uroot -prootpw phpipam <<'EOF'
SET FOREIGN_KEY_CHECKS=0;
DELETE FROM ipaddresses; DELETE FROM subnets; DELETE FROM sections;
DELETE FROM vlans; DELETE FROM api;
SET FOREIGN_KEY_CHECKS=1;
UPDATE settings SET api=1;
EOF
done

say "adding custom fields to all three instances"
# phpIPAM custom fields are plain extra columns; the name is whatever the
# admin typed. 'Owner'/'AssetTag' have no prefix (what the UI produces);
# 'custom_Notes' does. Both must replicate.
#
# Note instance 3 (the fan-in master) gets them too. Custom fields are
# SCHEMA, not data -- this tool replicates their values but cannot create
# the fields themselves, so a master missing them rejects every record
# that carries one.
for n in 1 2 3; do
docker compose exec -T "db${n}" mariadb -uroot -prootpw phpipam <<'EOF'
ALTER TABLE subnets     ADD COLUMN `Owner` varchar(128) DEFAULT NULL;
ALTER TABLE subnets     ADD COLUMN `custom_Notes` varchar(128) DEFAULT NULL;
ALTER TABLE ipaddresses ADD COLUMN `AssetTag` varchar(64) DEFAULT NULL;
EOF
done

say "diverging instance 2's id counters"
# So the same logical record cannot coincidentally share an id across
# the two instances -- see the note at the top of docker-compose.yml.
docker compose exec -T db2 mariadb -uroot -prootpw phpipam <<'EOF'
ALTER TABLE sections    AUTO_INCREMENT=500;
ALTER TABLE subnets     AUTO_INCREMENT=900;
ALTER TABLE ipaddresses AUTO_INCREMENT=7000;
ALTER TABLE vlans       AUTO_INCREMENT=300;
ALTER TABLE vlanDomains AUTO_INCREMENT=40;
ALTER TABLE vrf         AUTO_INCREMENT=60;
EOF

say "seeding instance 1 (source of truth)"
python3 - "$SRC_TOKEN" > /tmp/phpipam-seed1.sql <<'PY'
import ipaddress as ipa, sys
token = sys.argv[1]
d = lambda a: str(int(ipa.ip_address(a)))
print("USE phpipam;")
# READ-ONLY api app (app_permissions=1), app-code security.
print(f"INSERT INTO api (app_id,app_code,app_permissions,app_security,app_comment) "
      f"VALUES ('sync','{token}',1,'ssl_code','phpipam-sync exporter (read-only)');")
print("UPDATE api SET app_nest_custom_fields=1 WHERE app_id='sync';")
print("INSERT INTO sections (id,name,description,permissions,strictMode,`order`) "
      "VALUES (1,'Shared','Replicated to instance 2','{\"3\":\"1\"}',1,1);")
# A section that is NOT replicated -- proves scoping actually scopes.
print("INSERT INTO sections (id,name,description,permissions,strictMode,`order`) "
      "VALUES (2,'LocalOnly','Must never be replicated','{\"3\":\"1\"}',1,2);")
# An L2 domain scoped to the Shared section, plus VLANs inside it.
# vlanDomains.permissions holds a ";"-separated list of SECTION ids
# despite the name -- see phpIPAM's Sections::fetch_section_domains.
# Domain 1 ("default") belongs to every section implicitly.
print("INSERT INTO vlanDomains (id,name,description,permissions) "
      "VALUES (2,'Site-A','L2 domain for the Shared section','1');")
print("INSERT INTO vlans (vlanId,domainId,name,number,description) "
      "VALUES (10,1,'shared-vlan',100,'lab vlan');")
# A VLAN nothing references: it must replicate anyway.
print("INSERT INTO vlans (vlanId,domainId,name,number,description) "
      "VALUES (11,2,'voice',200,'unattached on purpose');")
print("INSERT INTO vlans (vlanId,domainId,name,number,description) "
      "VALUES (12,2,'mgmt',9,'single digit, to catch string sorting');")
# VRFs are scoped by a real `sections` column. One attached, one not.
print("INSERT INTO vrf (vrfId,name,rd,description,sections) "
      "VALUES (5,'CUST-A','65000:1','customer A','1');")
print("INSERT INTO vrf (vrfId,name,rd,description,sections) "
      "VALUES (6,'CUST-B','65000:2','unattached on purpose','1');")
# A supernet with no addresses of its own (this is what exposed
# phpIPAM's 404-on-empty-collection behaviour), a nested /24 with a
# VLAN, and an unrelated top-level /24.
print(f"INSERT INTO subnets (id,subnet,mask,sectionId,description,masterSubnetId,showName,state) "
      f"VALUES (11,'{d('10.20.0.0')}','16',1,'Shared supernet',0,1,2);")
print(f"INSERT INTO subnets (id,subnet,mask,sectionId,description,masterSubnetId,vlanId,pingSubnet,state) "
      f"VALUES (12,'{d('10.20.5.0')}','24',1,'Shared /24',11,10,1,2);")
print(f"INSERT INTO subnets (id,subnet,mask,sectionId,description,masterSubnetId,vrfId,state) "
      f"VALUES (13,'{d('10.30.0.0')}','24',1,'Standalone /24',0,5,2);")
print(f"INSERT INTO subnets (id,subnet,mask,sectionId,description,masterSubnetId,state) "
      f"VALUES (14,'{d('192.168.99.0')}','24',2,'Local only subnet',0,2);")
print("UPDATE subnets SET `Owner`='netops-team', custom_Notes='prefixed note' WHERE id=12;")
# IPv6, to prove the CIDR handling is not quietly IPv4-only.
print(f"INSERT INTO subnets (id,subnet,mask,sectionId,description,masterSubnetId,state) "
      f"VALUES (15,'{d('2001:db8:5::')}','64',1,'Shared v6 /64',0,2);")
print(f"INSERT INTO ipaddresses (id,subnetId,ip_addr,hostname,description,is_gateway,owner,state) "
      f"VALUES (106,15,'{d('2001:db8:5::1')}','v6-gw','v6 gateway',1,'netops',2);")
for i, sid, a, host, desc, gw, mac, owner, port in [
    (101,12,'10.20.5.2','gw-core','gateway router',1,'aa:bb:cc:dd:ee:02','netops','Gi0/0'),
    (102,12,'10.20.5.10','sw10','access switch',0,'aa:bb:cc:dd:ee:10','netops','Gi1/0/1'),
    (103,12,'10.20.5.20','printer','floor 2 printer',0,'aa:bb:cc:dd:ee:20','facilities',''),
    (104,13,'10.30.0.1','standalone-gw','standalone gateway',1,'','netops',''),
    (105,14,'192.168.99.5','local-host','must not replicate',0,'','local',''),
]:
    print(f"INSERT INTO ipaddresses (id,subnetId,ip_addr,hostname,description,is_gateway,mac,owner,port,state) "
          f"VALUES ({i},{sid},'{d(a)}','{host}','{desc}',{gw},'{mac}','{owner}','{port}',2);")
print("UPDATE ipaddresses SET AssetTag='ASSET-0042' WHERE id=101;")
PY
docker compose exec -T db1 mariadb -uroot -prootpw < /tmp/phpipam-seed1.sql
rm -f /tmp/phpipam-seed1.sql

say "seeding instance 2 (replica: empty 'Shared' section only)"
# The section must pre-exist: create_missing_sections is off by default.
docker compose exec -T db2 mariadb -uroot -prootpw phpipam <<EOF
INSERT INTO api (app_id,app_code,app_permissions,app_security,app_comment)
  VALUES ('sync','${DST_TOKEN}',2,'ssl_code','phpipam-sync importer (read-write)');
UPDATE api SET app_nest_custom_fields=1 WHERE app_id='sync';
INSERT INTO sections (name,description,permissions,strictMode,\`order\`)
  VALUES ('Shared','Replica of instance 1','{"3":"1"}',1,1);
EOF

say "seeding instance 3 (the fan-in MASTER: empty per-site sections)"
# One section PER SUBORDINATE. This is the whole point of the fan-in
# layout: each import owns exactly one section, so a subordinate can
# never see another's records as orphans.
docker compose exec -T db3 mariadb -uroot -prootpw phpipam <<EOF
INSERT INTO api (app_id,app_code,app_permissions,app_security,app_comment)
  VALUES ('sync','${DST_TOKEN}',2,'ssl_code','phpipam-sync master importer');
UPDATE api SET app_nest_custom_fields=1 WHERE app_id='sync';
INSERT INTO sections (name,description,permissions,strictMode,\`order\`)
  VALUES ('Site-A','Mirrored from subordinate A','{"3":"1"}',1,1);
INSERT INTO sections (name,description,permissions,strictMode,\`order\`)
  VALUES ('Site-B','Mirrored from subordinate B','{"3":"1"}',1,2);
EOF

say "verifying the APIs answer"
for port in 8443 8444 8445; do
    tok=$SRC_TOKEN; [ "$port" = 8443 ] || tok=$DST_TOKEN
    printf '  https://127.0.0.1:%s -> ' "$port"
    curl -sk -H "token: $tok" "https://127.0.0.1:$port/api/sync/sections/" \
        | python3 -c 'import sys,json;d=json.load(sys.stdin);print("OK,",len(d.get("data",[])),"section(s)" if d.get("success") else d)'
done

cat <<EOF

Lab is up. To run the sync end to end:

  export PHPIPAM_SRC_TOKEN=$SRC_TOKEN
  export PHPIPAM_DST_TOKEN=$DST_TOKEN
  ./verify.sh

Fan-in lab (instances 1+2 as subordinates, 3 as master):
  ./verify-fanin.sh

Tear down with:  docker compose down
EOF
