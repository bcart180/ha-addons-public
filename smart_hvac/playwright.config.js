module.exports = {
  testDir: "./tests/ui",
  timeout: 180000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: "list",
  projects: [
    { name: "webkit", use: { browserName: "webkit" } },
    { name: "chromium", use: { browserName: "chromium" } },
  ],
};
