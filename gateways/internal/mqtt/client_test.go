package mqtt

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"io"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// login is what a fake broker saw in a CONNECT.
type login struct{ clientID, username, password string }

// fakeBroker accepts one connection on ln, reads CONNECT, answers with returnCode,
// and reports the login it saw.
func fakeBroker(t *testing.T, ln net.Listener, returnCode byte) <-chan login {
	t.Helper()
	seen := make(chan login, 1)
	go func() {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		conn.SetDeadline(time.Now().Add(5 * time.Second))
		header := make([]byte, 2)
		if _, err := io.ReadFull(conn, header); err != nil || header[0] != packetConnect {
			return
		}
		body := make([]byte, header[1]) // test packets are under 128 bytes
		if _, err := io.ReadFull(conn, body); err != nil {
			return
		}
		flags := body[7]
		rest := body[10:]
		next := func() string {
			n := int(rest[0])<<8 | int(rest[1])
			s := string(rest[2 : 2+n])
			rest = rest[2+n:]
			return s
		}
		var l login
		l.clientID = next()
		if flags&0x80 != 0 {
			l.username = next()
		}
		if flags&0x40 != 0 {
			l.password = next()
		}
		seen <- l
		conn.Write([]byte{packetConnAck, 2, 0, returnCode})
		io.Copy(io.Discard, conn) // hold the connection while the client publishes
	}()
	return seen
}

func writeFile(t *testing.T, name, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), name)
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestThePasswordFileReachesTheBroker(t *testing.T) {
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	defer ln.Close()
	seen := fakeBroker(t, ln, 0)
	pw := writeFile(t, "gw.password", "s3cret-from-file\n")
	c, err := New("mqtt://ignored-user@"+ln.Addr().String(), "gw-1", Options{Username: "gw-dev", PasswordFile: pw})
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	if err := c.Publish("s2/v1/t/CommercialDrone/DRN-0001/telemetry", []byte("{}")); err != nil {
		t.Fatal(err)
	}
	if l := <-seen; l != (login{"gw-1", "gw-dev", "s3cret-from-file"}) {
		t.Errorf("broker saw %+v", l)
	}
}

func TestARefusedLoginIsExplained(t *testing.T) {
	for code, want := range map[byte]string{4: "username or password is wrong", 5: "wrong username or password"} {
		ln, _ := net.Listen("tcp", "127.0.0.1:0")
		fakeBroker(t, ln, code)
		c, err := New("mqtt://gw-dev:wrong@"+ln.Addr().String(), "gw-1")
		if err != nil {
			t.Fatal(err)
		}
		err = c.Publish("x", nil)
		if err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("return code %d: error %v, want one containing %q", code, err, want)
		}
		ln.Close()
	}
}

func TestOptionsThatCannotWorkAreRefused(t *testing.T) {
	ca := writeFile(t, "ca.crt", "not a certificate")
	empty := writeFile(t, "empty.password", "\n")
	cases := map[string]struct {
		url  string
		opts Options
		want string
	}{
		"password, no user": {"mqtt://localhost", Options{PasswordFile: writeFile(t, "p", "x")}, "without a username"},
		"empty password":    {"mqtt://localhost", Options{Username: "u", PasswordFile: empty}, "is empty"},
		"missing file":      {"mqtt://localhost", Options{Username: "u", PasswordFile: "/nonexistent/p"}, "password file"},
		"CA on plain mqtt":  {"mqtt://localhost", Options{CAFile: ca}, "needs an mqtts:// broker"},
		"CA not PEM":        {"mqtts://localhost", Options{CAFile: ca}, "no PEM certificate"},
	}
	for name, tc := range cases {
		if _, err := New(tc.url, "id", tc.opts); err == nil || !strings.Contains(err.Error(), tc.want) {
			t.Errorf("%s: error %v, want one containing %q", name, err, tc.want)
		}
	}
}

// TLS: the client trusts the CA it is given and nothing else.
func TestTLSTrustsOnlyTheGivenCA(t *testing.T) {
	caPEM, serverCert := testPKI(t)
	ln, err := tls.Listen("tcp", "127.0.0.1:0", &tls.Config{Certificates: []tls.Certificate{serverCert}})
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	_, port, _ := net.SplitHostPort(ln.Addr().String())
	url := "mqtts://gw-dev@localhost:" + port
	pw := writeFile(t, "pw", "s3cret")

	seen := fakeBroker(t, ln, 0)
	c, err := New(url, "gw-1", Options{PasswordFile: pw, CAFile: writeFile(t, "ca.crt", caPEM)})
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Publish("x", []byte("{}")); err != nil {
		t.Fatalf("with the CA: %v", err)
	}
	if l := <-seen; l.password != "s3cret" {
		t.Errorf("broker saw %+v", l)
	}
	c.Close()

	fakeBroker(t, ln, 0)
	c, err = New(url, "gw-1", Options{PasswordFile: pw}) // system roots only
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Publish("x", nil); err == nil || !strings.Contains(err.Error(), "certificate") {
		t.Fatalf("without the CA the broker must not be trusted; got %v", err)
	}
}

// testPKI makes a CA and a broker certificate for localhost signed by it.
func testPKI(t *testing.T) (string, tls.Certificate) {
	t.Helper()
	caKey, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	caTmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "test CA"},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
		IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign,
	}
	caDER, err := x509.CreateCertificate(rand.Reader, caTmpl, caTmpl, &caKey.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	ca, _ := x509.ParseCertificate(caDER)
	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(2), Subject: pkix.Name{CommonName: "broker"}, DNSNames: []string{"localhost"},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
		KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, ca, &key.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	caPEM := string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER}))
	return caPEM, tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}
}
