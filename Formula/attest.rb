class Attest < Formula
  desc "Local completion-attestation gate for Claude Code subagents"
  homepage "https://github.com/ek33450505/attest"
  url "https://github.com/ek33450505/attest/archive/refs/tags/v0.1.0.tar.gz"
  # sha256 of the v0.1.0 github archive tarball (archive/refs/tags); verified byte-stable.
  sha256 "9c47dc0cfd75311d73c91e89e9bac6375ceaf3946ac913b09fc66d9cbc13bff8"
  license "MIT"

  depends_on "python3" => :required

  def install
    # Stdlib-only: install the package + hooks + installer into libexec, then a
    # thin bin wrapper that runs the in-tree bin/attest (which resolves the
    # package from its own libexec location).
    libexec.install "attest", "hooks", "bin", "install.sh", "LICENSE", "README.md"

    (bin/"attest").write <<~SH
      #!/bin/bash
      exec python3 "#{libexec}/bin/attest" "$@"
    SH
  end

  def caveats
    <<~EOS
      The `attest` CLI is installed. To wire the SubagentStart/SubagentStop hooks
      into Claude Code, choose one:

        Plugin (recommended):
          /plugin marketplace add https://github.com/ek33450505/attest
          /plugin install attest@attest

        Manual (writes ~/.claude/settings.json):
          bash #{libexec}/install.sh

      Enforcement is OFF by default. Enable it per-session/environment with:
          export ATTEST_ENFORCE=1
    EOS
  end

  test do
    system "#{bin}/attest", "--version"
  end
end
