// starttls-guard: per-connection IMAP proxy run by imap-gate (socat SYSTEM: stdio = client).
//
// Proton Bridge v3.27.1 in STARTTLS mode (vault Settings.IMAPSSL=false, the default)
// advertises AUTH=PLAIN before STARTTLS and evaluates a cleartext LOGIN. This guard sits
// between the client and Bridge (127.0.0.1:1143) and, until STARTTLS succeeds:
//   - strips AUTH=* from every CAPABILITY list and adds LOGINDISABLED (RFC 3501 6.2.3)
//   - answers LOGIN / AUTHENTICATE itself with NO [PRIVACYREQUIRED]; they never reach Bridge
//     (with a literal attached the connection is closed, so literal bytes are never parsed)
//   - after "tag OK" to STARTTLS, refuses any bytes already buffered on either side
//     (STARTTLS command/response injection) and then becomes a transparent byte pipe.
//
// If the client speaks TLS first (0x16) or Bridge does not send an IMAP greeting (Bridge
// switched to implicit SSL), it is a transparent pipe from the start. It never sees or
// logs credentials.
package main

import (
	"bufio"
	"bytes"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const (
	maxLine    = 64 * 1024
	maxLiteral = 1 << 20
)

var literalRe = regexp.MustCompile(`\{(\d+)\+?\}\r?\n$`)

// anyLiteralRe matches an IMAP literal marker anywhere in a client line ({n} or {n+}).
var anyLiteralRe = regexp.MustCompile(`\{\d+\+?\}`)

// PreTLSTimeout: the connection is closed if TLS (STARTTLS or implicit) is not reached in time.
var PreTLSTimeout = 60 * time.Second

// validTag: RFC 3501 tag = 1*<any ASTRING-CHAR except "+">; "*" and "+" are never valid.
func validTag(t string) bool {
	if t == "" {
		return false
	}
	for i := 0; i < len(t); i++ {
		c := t[i]
		if c <= 0x20 || c >= 0x7f || strings.IndexByte("(){%*\"\\+", c) >= 0 {
			return false
		}
	}
	return true
}

func logf(format string, a ...any) {
	fmt.Fprintf(os.Stderr, "[proton-bridge] starttls-guard: "+format+"\n", a...)
}

// rewriteCaps removes AUTH=* and adds LOGINDISABLED in a CAPABILITY list (untagged
// "* CAPABILITY ..." or a "[CAPABILITY ...]" response code). Other lines are unchanged.
func rewriteCaps(line []byte) []byte {
	s := string(line)
	eol := ""
	for strings.HasSuffix(s, "\r") || strings.HasSuffix(s, "\n") {
		eol = s[len(s)-1:] + eol
		s = s[:len(s)-1]
	}
	fix := func(list string) string {
		var out []string
		has := false
		for _, t := range strings.Fields(list) {
			u := strings.ToUpper(t)
			if strings.HasPrefix(u, "AUTH=") {
				continue
			}
			if u == "LOGINDISABLED" {
				has = true
			}
			out = append(out, t)
		}
		if !has {
			out = append(out, "LOGINDISABLED")
		}
		return strings.Join(out, " ")
	}
	up := strings.ToUpper(s)
	if strings.HasPrefix(up, "* CAPABILITY ") {
		return []byte("* CAPABILITY " + fix(s[len("* CAPABILITY "):]) + eol)
	}
	if i := strings.Index(up, "[CAPABILITY "); i >= 0 {
		j := strings.Index(s[i:], "]")
		if j > 0 {
			inner := s[i+len("[CAPABILITY ") : i+j]
			return []byte(s[:i] + "[CAPABILITY " + fix(inner) + s[i+j:] + eol)
		}
	}
	return line
}

func readLine(r *bufio.Reader) ([]byte, error) {
	var buf []byte
	for {
		chunk, err := r.ReadSlice('\n')
		buf = append(buf, chunk...)
		if len(buf) > maxLine {
			return nil, errors.New("line too long")
		}
		if err == bufio.ErrBufferFull {
			continue
		}
		return buf, err
	}
}

func literalLen(line []byte) (int64, bool) {
	m := literalRe.FindSubmatch(line)
	if m == nil {
		return 0, false
	}
	n, err := strconv.ParseInt(string(m[1]), 10, 64)
	if err != nil || n > maxLiteral {
		return -1, true
	}
	return n, true
}

// tagCmd returns the tag and upper-cased command of a client line.
func tagCmd(line []byte) (string, string) {
	f := strings.Fields(string(bytes.TrimRight(line, "\r\n")))
	if len(f) < 2 {
		if len(f) == 1 {
			return f[0], ""
		}
		return "", ""
	}
	cmd := strings.ToUpper(f[1])
	if cmd == "UID" && len(f) > 2 {
		cmd = "UID " + strings.ToUpper(f[2])
	}
	return f[0], cmd
}

type decision struct{ ok bool }

// Guard proxies one connection. client reads from cr and writes to cw.
func Guard(cr io.Reader, cw io.Writer, up net.Conn) error {
	defer up.Close()
	c := bufio.NewReaderSize(cr, 4096)
	u := bufio.NewReaderSize(up, 4096)
	done := make(chan error, 3)

	modeCh := make(chan bool, 1) // true = line mode (plain IMAP greeting seen)
	starttlsTag := make(chan string, 1)
	tlsDecision := make(chan decision, 1)
	tlsUp := make(chan struct{}, 1) // client side became transparent (TLS reached)
	timer := time.AfterFunc(PreTLSTimeout, func() {
		logf("closed: no TLS within %s", PreTLSTimeout)
		io.WriteString(cw, "* BYE Timeout waiting for STARTTLS\r\n")
		done <- errors.New("pre-TLS timeout")
	})
	defer timer.Stop()
	go func() { <-tlsUp; timer.Stop() }()

	// server -> client
	go func() {
		b, err := u.Peek(1)
		if err != nil {
			modeCh <- false
			done <- err
			return
		}
		line := b[0] == '*'
		modeCh <- line
		if !line {
			_, err = io.Copy(cw, u)
			done <- err
			return
		}
		pending := ""
		for {
			select {
			case t := <-starttlsTag:
				pending = t
			default:
			}
			l, err := readLine(u)
			if len(l) > 0 {
				if pending == "" {
					// a STARTTLS tag may have been set while we were blocked in readLine
					select {
					case t := <-starttlsTag:
						pending = t
					default:
					}
				}
				out := rewriteCaps(l)
				if _, werr := cw.Write(out); werr != nil {
					done <- werr
					return
				}
				if n, ok := literalLen(l); ok {
					if n < 0 {
						done <- errors.New("server literal too large")
						return
					}
					if _, err := io.CopyN(cw, u, n); err != nil {
						done <- err
						return
					}
				}
				if pending != "" {
					tag, rest := tagCmd(l)
					if tag == pending {
						ok := strings.HasPrefix(rest, "OK")
						if ok && u.Buffered() > 0 {
							tlsDecision <- decision{false}
							done <- errors.New("server sent data after STARTTLS OK (injection); closing")
							return
						}
						tlsDecision <- decision{ok}
						pending = ""
						if ok {
							_, err = io.Copy(cw, u)
							done <- err
							return
						}
					}
				}
			}
			if err != nil {
				done <- err
				return
			}
		}
	}()

	// client -> server
	go func() {
		b, err := c.Peek(1)
		if err != nil {
			done <- err
			return
		}
		if b[0] == 0x16 { // TLS ClientHello: implicit TLS, nothing to inspect
			tlsUp <- struct{}{}
			_, err = io.Copy(up, c)
			done <- err
			return
		}
		if !<-modeCh {
			tlsUp <- struct{}{}
			_, err = io.Copy(up, c)
			done <- err
			return
		}
		for {
			l, err := readLine(c)
			if len(l) > 0 {
				tag, cmd := tagCmd(l)
				if !validTag(tag) {
					logf("closed: invalid tag before STARTTLS")
					io.WriteString(cw, "* BYE Invalid tag\r\n")
					done <- errors.New("invalid pre-TLS tag")
					return
				}
				if anyLiteralRe.Match(l) {
					// G1: no literals before TLS (they can smuggle commands / credentials).
					logf("closed: literal before STARTTLS (%s)", cmd)
					io.WriteString(cw, "* BYE [PRIVACYREQUIRED] Literals not allowed before STARTTLS\r\n")
					done <- errors.New("closed: pre-TLS literal")
					return
				}
				switch cmd {
				case "LOGIN", "AUTHENTICATE":
					logf("refused %s before STARTTLS (not forwarded to Bridge)", cmd)
					if _, werr := io.WriteString(cw, tag+" NO [PRIVACYREQUIRED] Plaintext authentication disallowed; use STARTTLS or SSL/TLS\r\n"); werr != nil {
						done <- werr
						return
					}
					continue
				case "STARTTLS":
					starttlsTag <- tag
					if _, werr := up.Write(l); werr != nil {
						done <- werr
						return
					}
					d := <-tlsDecision
					if !d.ok {
						continue
					}
					if c.Buffered() > 0 {
						done <- errors.New("client sent data after STARTTLS before TLS (injection); closing")
						return
					}
					tlsUp <- struct{}{}
					_, err = io.Copy(up, c)
					done <- err
					return
				}
				if _, werr := up.Write(l); werr != nil {
					done <- werr
					return
				}
			}
			if err != nil {
				done <- err
				return
			}
		}
	}()

	err := <-done
	return err
}

func main() {
	addr := "127.0.0.1:1143"
	if len(os.Args) > 1 {
		addr = os.Args[1]
	}
	up, err := net.DialTimeout("tcp4", addr, 10*time.Second)
	if err != nil {
		logf("cannot reach Bridge at %s: %v", addr, err)
		os.Exit(1)
	}
	Guard(os.Stdin, os.Stdout, up)
	os.Exit(0)
}
