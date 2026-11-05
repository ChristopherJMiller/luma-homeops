// Connect probe for the NetworkPolicy canary and the forgejo-runner check (design §9.3).
//   node netpol-connect.js tcp HOST PORT
//   node netpol-connect.js dns HOST
//   node -e "$(cat netpol-connect.js)" tcp HOST PORT      (node -e has no require.main)
// Prints open|refused|timeout|other:<code> and exits 0. A refusal means the packet got through.
// Any other argument list prints other:usage: never a dial to localhost, never a stack trace.
const net = require("net");
const dns = require("dns");
const args = process.argv.slice(require.main === module ? 2 : 1);
const [mode, host, port] = args;
const done = (s) => { console.log(s); process.exit(0); };
const isPort = (p) => /^[0-9]{1,5}$/.test(p) && Number(p) >= 1 && Number(p) <= 65535;
if (mode === "tcp" && args.length === 3 && host && isPort(port)) {
  const s = net.connect({ host, port: Number(port), timeout: 3000 });
  s.on("connect", () => done("open"));
  s.on("timeout", () => done("timeout"));
  s.on("error", (e) => {
    if (e.code === "ECONNREFUSED") done("refused");
    else if (e.code === "ETIMEDOUT") done("timeout");
    else done("other:" + (e.code || "error"));
  });
} else if (mode === "dns" && args.length === 2 && host) {
  dns.promises.lookup(host).then(() => done("open"), (e) => done("other:" + (e.code || "error")));
  setTimeout(() => done("timeout"), 3000).unref();
} else {
  done("other:usage");
}
