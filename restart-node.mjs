import { argv } from "node:process";
import { execSync } from "node:child_process";

const execToConsole = (command) => {
    execSync(command, { stdio: "inherit" });
};

const node = argv[2];

if (!node) {
    console.log("restart-node.mjs <node>");
}

const user = execSync("whoami", { encoding: "UTF-8" }).trim();

console.log("Verifying can successfully execute ssh commands to IP");

const test = execSync(`ssh ${user}@${node} "echo 1234"`, { encoding: "UTF-8" }).trim();

if (test !== "1234") {
    throw "Failed to connect to node, verify connection";
}

console.log("SSH was successful!");

console.log(`Draining ${node} with a timeout of 100 seconds`);

execToConsole(`kubectl drain ${node} --grace-period=100 --ignore-daemonsets --delete-emptydir-data`);

console.log("Completed! Rebooting...");

try {
    execToConsole(`ssh ${user}@${node} "sudo reboot now"`);
} catch (e) {
    console.log(e);
}

console.log("Polling until rebooted");

let attempt = 0;

while (true) {
    attempt += 1;
    console.log(`Attempt ${attempt}`);

    const test = execSync(`ssh ${user}@${node} "echo 1234"`, { encoding: "UTF-8" }).trim();

    if (test === "1234") {
        break;
    }
}

console.log("Uncordoning Node");

execToConsole(`kubectl uncordon ${node}`);

console.log("Done!");
