#!/usr/bin/env bash
# Regenerate the TLS trust fixtures.
#
# WHY THIS EXISTS. The fixtures were committed as PEMs with no way to rebuild
# them, and OpenSSL moved: 3.6.2 refuses a chain with no Authority Key
# Identifier, so on this box EVERY case failed with "Missing Authority Key
# Identifier" -- including the well-formed one the suite expects to be
# ACCEPTED. The four "is this root refused as a trust anchor" cases were all
# passing their `err is not None` check for the wrong reason, and the suite
# had been on the known-failures list ever since.
#
# The shapes under test (tests/test_tls_trust.py):
#   good            CA:TRUE, keyUsage keyCertSign        -> OpenSSL accepts
#   ku_no_certsign  CA:TRUE, keyUsage without keyCertSign -> refused
#   no_ca_markers   no basicConstraints, no keyUsage      -> refused
#   ca_false        CA:FALSE                              -> refused
# Every certificate carries subjectKeyIdentifier + authorityKeyIdentifier, so
# the refusals are about the CA markers and nothing else.
#
# TEST-ONLY keys. Valid for 100 years so the suite does not rot on a date.
set -euo pipefail
cd "$(dirname "$0")"

DAYS=36500
SUBJ_LEAF="/CN=localhost"

gen_root() {                     # $1 = name, $2 = extra extensions
    local name=$1 exts=$2
    openssl req -x509 -newkey rsa:2048 -nodes -keyout "root_${name}.key" \
        -out "root_${name}.pem" -days $DAYS -subj "/CN=Domestique TEST-ONLY root ${name}" \
        -addext "subjectKeyIdentifier=hash" \
        -addext "authorityKeyIdentifier=keyid:always" \
        ${exts:+-addext "$exts"} \
        ${exts2:+-addext "$exts2"} 2>/dev/null
}

# One leaf key, four leaves (one per root).
openssl genrsa -out leaf.key 2048 2>/dev/null
openssl req -new -key leaf.key -out leaf.csr -subj "$SUBJ_LEAF" 2>/dev/null

sign_leaf() {                    # $1 = root name -> leaf_<name>.pem
    local name=$1
    cat > "ext_${name}.cnf" <<EOF
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:localhost, IP:127.0.0.1
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
EOF
    openssl x509 -req -in leaf.csr -CA "root_${name}.pem" -CAkey "root_${name}.key" \
        -CAcreateserial -out "leaf_${name}.pem" -days $DAYS \
        -extfile "ext_${name}.cnf" 2>/dev/null
    rm -f "ext_${name}.cnf"
}

exts2=""
exts2="keyUsage=critical,keyCertSign,cRLSign" gen_root good "basicConstraints=critical,CA:TRUE"
exts2="keyUsage=critical,digitalSignature"    gen_root ku_no_certsign "basicConstraints=critical,CA:TRUE"
exts2=""                                      gen_root no_ca_markers ""
exts2="keyUsage=critical,keyCertSign,cRLSign" gen_root ca_false "basicConstraints=critical,CA:FALSE"

for n in good ku_no_certsign no_ca_markers ca_false; do
    sign_leaf "$n"
done
rm -f leaf.csr root_*.srl
# The root private keys are not needed by the suite; only the certificates are.
rm -f root_*.key
echo "regenerated: $(ls root_*.pem leaf_*.pem leaf.key | tr '\n' ' ')"
