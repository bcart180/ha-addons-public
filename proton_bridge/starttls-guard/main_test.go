package main

import (
	"bufio"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"math/big"
	"net"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeBridge mimics Bridge v3.27.1 STARTTLS mode (Pi test 2026-10-30: pre-TLS CAPABILITY
// "AUTH=PLAIN ID IDLE IMAP4rev1 STARTTLS", and it evaluates a cleartext LOGIN).
type fakeBridge struct {
	mu       sync.Mutex
	plainCmd []string // every command Bridge saw before TLS
	tlsCmd   []string
}

func selfSigned(t *testing.T) tls.Certificate {
	k, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	tpl := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "127.0.0.1"},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour)}
	der, err := x509.CreateCertificate(rand.Reader, tpl, tpl, &k.PublicKey, k)
	if err != nil {
		t.Fatal(err)
	}
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: k}
}

func (f *fakeBridge) serve(t *testing.T, conn net.Conn, cert tls.Certificate) {
	defer conn.Close()
	conn.Write([]byte("* OK [CAPABILITY AUTH=PLAIN ID IDLE IMAP4rev1 STARTTLS] Proton Mail Bridge 3.27.1 - gluon session ID 1\r\n"))
	r := bufio.NewReader(conn)
	var w net.Conn = conn
	inTLS := false
	for {
		l, err := r.ReadString('\n')
		if err != nil {
			return
		}
		tag, cmd := tagCmd([]byte(l))
		f.mu.Lock()
		if inTLS {
			f.tlsCmd = append(f.tlsCmd, cmd)
		} else {
			f.plainCmd = append(f.plainCmd, cmd)
		}
		f.mu.Unlock()
		switch cmd {
		case "CAPABILITY":
			if inTLS {
				w.Write([]byte("* CAPABILITY AUTH=PLAIN ID IDLE IMAP4rev1\r\n"))
			} else {
				w.Write([]byte("* CAPABILITY AUTH=PLAIN ID IDLE IMAP4rev1 STARTTLS\r\n"))
			}
			w.Write([]byte(tag + " OK CAPABILITY completed\r\n"))
		case "STARTTLS":
			w.Write([]byte(tag + " OK Begin TLS negotiation now\r\n"))
			ts := tls.Server(conn, &tls.Config{Certificates: []tls.Certificate{cert}})
			if err := ts.Handshake(); err != nil {
				return
			}
			w, r, inTLS = ts, bufio.NewReader(ts), true
		case "LOGIN":
			w.Write([]byte(tag + " NO no such user\r\n"))
		case "LOGOUT":
			w.Write([]byte("* BYE\r\n" + tag + " OK\r\n"))
			return
		default:
			w.Write([]byte(tag + " BAD\r\n"))
		}
	}
}

// harness: client <-pipe-> Guard <-pipe-> fakeBridge
func harness(t *testing.T) (net.Conn, *bufio.Reader, *fakeBridge) {
	f := &fakeBridge{}
	cert := selfSigned(t)
	gUp, bSide := net.Pipe()
	go f.serve(t, bSide, cert)
	cli, gCli := net.Pipe()
	go func() { Guard(gCli, gCli, gUp); gCli.Close() }()
	cli.SetDeadline(time.Now().Add(10 * time.Second))
	return cli, bufio.NewReader(cli), f
}

func send(t *testing.T, c net.Conn, s string) {
	if _, err := c.Write([]byte(s)); err != nil {
		t.Fatal(err)
	}
}

func readUntilTag(t *testing.T, r *bufio.Reader, tag string) []string {
	var out []string
	for {
		l, err := r.ReadString('\n')
		if err != nil {
			t.Fatalf("read: %v (got %q)", err, out)
		}
		out = append(out, strings.TrimRight(l, "\r\n"))
		if strings.HasPrefix(l, tag+" ") || strings.HasPrefix(l, "* BYE") {
			return out
		}
	}
}

func TestPreTLSCapabilityAndLoginRefused(t *testing.T) {
	c, r, f := harness(t)
	g, _ := r.ReadString('\n')
	if strings.Contains(g, "AUTH=") || !strings.Contains(g, "LOGINDISABLED") || !strings.Contains(g, "STARTTLS") {
		t.Fatalf("greeting not rewritten: %q", g)
	}
	send(t, c, "a1 CAPABILITY\r\n")
	caps := readUntilTag(t, r, "a1")
	if strings.Contains(caps[0], "AUTH=") || !strings.Contains(caps[0], "LOGINDISABLED") {
		t.Fatalf("CAPABILITY not rewritten: %q", caps)
	}
	send(t, c, "a2 LOGIN dummyuser dummypass\r\n")
	resp := readUntilTag(t, r, "a2")
	if !strings.HasPrefix(resp[len(resp)-1], "a2 NO [PRIVACYREQUIRED]") {
		t.Fatalf("LOGIN not refused: %q", resp)
	}
	send(t, c, "a3 AUTHENTICATE PLAIN AGR1bW15AGR1bW15\r\n")
	resp = readUntilTag(t, r, "a3")
	if !strings.HasPrefix(resp[len(resp)-1], "a3 NO [PRIVACYREQUIRED]") {
		t.Fatalf("AUTHENTICATE not refused: %q", resp)
	}
	send(t, c, "a4 LOGOUT\r\n")
	readUntilTag(t, r, "a4")
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, cmd := range f.plainCmd {
		if cmd == "LOGIN" || cmd == "AUTHENTICATE" {
			t.Fatalf("Bridge saw cleartext %s: %v", cmd, f.plainCmd)
		}
	}
	t.Logf("Bridge saw before TLS: %v", f.plainCmd)
}

func TestLoginLiteralClosesConnection(t *testing.T) {
	c, r, f := harness(t)
	r.ReadString('\n')
	send(t, c, "a1 LOGIN {9+}\r\ndummyuser {9+}\r\ndummypass\r\n")
	l, _ := r.ReadString('\n')
	if !strings.HasPrefix(l, "* BYE [PRIVACYREQUIRED]") {
		t.Fatalf("expected BYE, got %q", l)
	}
	time.Sleep(50 * time.Millisecond)
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, cmd := range f.plainCmd {
		if cmd != "" {
			t.Fatalf("Bridge saw %v", f.plainCmd)
		}
	}
}

func TestStartTLSThenLoginPassesThrough(t *testing.T) {
	c, r, f := harness(t)
	r.ReadString('\n')
	send(t, c, "s1 STARTTLS\r\n")
	resp := readUntilTag(t, r, "s1")
	if !strings.HasPrefix(resp[len(resp)-1], "s1 OK") {
		t.Fatalf("STARTTLS: %q", resp)
	}
	tc := tls.Client(c, &tls.Config{InsecureSkipVerify: true})
	if err := tc.Handshake(); err != nil {
		t.Fatalf("TLS handshake through guard: %v", err)
	}
	tr := bufio.NewReader(tc)
	tc.Write([]byte("t1 CAPABILITY\r\n"))
	caps := readUntilTag(t, tr, "t1")
	if !strings.Contains(caps[0], "AUTH=PLAIN") {
		t.Fatalf("post-TLS caps must be untouched: %q", caps)
	}
	tc.Write([]byte("t2 LOGIN dummyuser dummypass\r\n"))
	resp = readUntilTag(t, tr, "t2")
	if resp[len(resp)-1] != "t2 NO no such user" {
		t.Fatalf("post-TLS LOGIN should reach Bridge: %q", resp)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.tlsCmd) < 2 || f.tlsCmd[1] != "LOGIN" {
		t.Fatalf("Bridge TLS cmds: %v", f.tlsCmd)
	}
}

func TestStartTLSInjectionRejected(t *testing.T) {
	c, r, f := harness(t)
	r.ReadString('\n')
	// pipelined plaintext after STARTTLS (CVE-2011-0411 class) must never reach Bridge
	go c.Write([]byte("s1 STARTTLS\r\nx1 LOGIN dummyuser dummypass\r\n"))
	time.Sleep(300 * time.Millisecond)
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, cmd := range append(f.plainCmd, f.tlsCmd...) {
		if cmd == "LOGIN" {
			t.Fatalf("injected LOGIN reached Bridge: plain=%v tls=%v", f.plainCmd, f.tlsCmd)
		}
	}
}

func TestRewriteCaps(t *testing.T) {
	cases := map[string]string{
		"* CAPABILITY AUTH=PLAIN ID IDLE IMAP4rev1 STARTTLS\r\n":               "* CAPABILITY ID IDLE IMAP4rev1 STARTTLS LOGINDISABLED\r\n",
		"* OK [CAPABILITY IMAP4rev1 auth=plain STARTTLS LOGINDISABLED] hi\r\n": "* OK [CAPABILITY IMAP4rev1 STARTTLS LOGINDISABLED] hi\r\n",
		"a1 OK done\r\n": "a1 OK done\r\n",
	}
	for in, want := range cases {
		if got := string(rewriteCaps([]byte(in))); got != want {
			t.Errorf("rewriteCaps(%q) = %q, want %q", in, got, want)
		}
	}
}

func noBridgeCmd(t *testing.T, f *fakeBridge, bad ...string) {
	time.Sleep(50 * time.Millisecond)
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, cmd := range append(f.plainCmd, f.tlsCmd...) {
		for _, b := range bad {
			if cmd == b {
				t.Fatalf("Bridge saw %s: plain=%v", b, f.plainCmd)
			}
		}
	}
}

func expectClosed(t *testing.T, r *bufio.Reader, prefix string) {
	l, _ := r.ReadString('\n')
	if !strings.HasPrefix(l, prefix) {
		t.Fatalf("expected %q, got %q", prefix, l)
	}
	if _, err := r.ReadString('\n'); err == nil {
		t.Fatalf("connection not closed after %q", prefix)
	}
}

// G1: a literal on ANY pre-TLS command must close the connection (MA repro).
func TestG1LiteralSmuggleViaNOOP(t *testing.T) {
	for _, lit := range []string{"{14}", "{14+}"} {
		c, r, f := harness(t)
		r.ReadString('\n')
		go c.Write([]byte("a NOOP " + lit + "\r\nb LOGIN u p\r\n"))
		expectClosed(t, r, "* BYE [PRIVACYREQUIRED]")
		noBridgeCmd(t, f, "NOOP", "LOGIN")
	}
}

// G2: tags "*", "+" and non-atom tags are refused before TLS.
func TestG2InvalidTags(t *testing.T) {
	for _, tag := range []string{"*", "+", "a(b", "a{b", "a%b", "a\"b", "a\\b", "+x"} {
		c, r, f := harness(t)
		r.ReadString('\n')
		go c.Write([]byte(tag + " CAPABILITY\r\n"))
		expectClosed(t, r, "* BYE Invalid tag")
		noBridgeCmd(t, f, "CAPABILITY")
	}
	for _, tag := range []string{"a1", "A.001", "x-9"} {
		if !validTag(tag) {
			t.Errorf("valid tag %q rejected", tag)
		}
	}
}

// G3: no TLS within PreTLSTimeout -> BYE + close; a TLS session outlives the deadline.
func TestG3PreTLSDeadline(t *testing.T) {
	old := PreTLSTimeout
	PreTLSTimeout = 300 * time.Millisecond
	defer func() { PreTLSTimeout = old }()

	c, r, _ := harness(t)
	r.ReadString('\n')
	start := time.Now()
	expectClosed(t, r, "* BYE Timeout")
	if d := time.Since(start); d > 2*time.Second {
		t.Fatalf("deadline took %v", d)
	}
	_ = c

	c, r, _ = harness(t)
	r.ReadString('\n')
	send(t, c, "s1 STARTTLS\r\n")
	readUntilTag(t, r, "s1")
	tc := tls.Client(c, &tls.Config{InsecureSkipVerify: true})
	if err := tc.Handshake(); err != nil {
		t.Fatal(err)
	}
	time.Sleep(600 * time.Millisecond)
	tc.Write([]byte("t1 CAPABILITY\r\n"))
	if caps := readUntilTag(t, bufio.NewReader(tc), "t1"); !strings.HasPrefix(caps[len(caps)-1], "t1 OK") {
		t.Fatalf("TLS session killed by pre-TLS deadline: %q", caps)
	}
}
