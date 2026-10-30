# Runs the "Register bundle ID" step of build-ipa.yml (the Ruby heredoc that reconciles the derived bundle ID against App Store Connect by
# name) with a stand-in for api.appstoreconnect.apple.com, and checks which bundle ID the build ends up with.
#   ruby test/reconcile_bundle_id_test.rb
require "minitest/autorun"
require "json"
require "net/http"
require "openssl"
require "tmpdir"
require "stringio"

ROOT = File.expand_path("..", __dir__)
WORKFLOW = File.read(File.join(ROOT, ".github/workflows/build-ipa.yml")).gsub("\r\n", "\n")

# The heredoc that holds the name lookup, with the workflow's indentation removed.
def reconcile_script
  at = WORKFLOW.index("filter[name]")
  start = WORKFLOW.rindex("ruby - <<'RUBY'\n", at) + "ruby - <<'RUBY'\n".length
  body = WORKFLOW[start...WORKFLOW.index("RUBY\n", start)]
  indent = body.lines.reject { |l| l.strip.empty? }.map { |l| l[/\A */].length }.min
  body.lines.map { |l| l.strip.empty? ? l : l[indent..] }.join
end

# Answers like App Store Connect: bundle IDs register, and the name filter lists `apps`.
module FakeAppStoreConnect
  class << self
    attr_accessor :apps
  end

  def request(req)
    path = req.path
    body = path.start_with?("/v1/bundleIds") ? { "data" => {} } : { "data" => FakeAppStoreConnect.apps }
    Struct.new(:code, :body).new(path.start_with?("/v1/bundleIds") ? "201" : "200", body.to_json)
  end
end
Net::HTTP.prepend(FakeAppStoreConnect)

class ReconcileBundleIdTest < Minitest::Test
  OWN = "com.frontendx.job-8229fa64-49b4-490f-9857-841b4f083204".freeze
  OTHER_JOB = "com.frontendx.job-2d31769a-1e7d-42bc-b2dc-8408f6d1bded".freeze

  def app(name, bundle)
    { "id" => "1", "attributes" => { "name" => name, "bundleId" => bundle } }
  end

  # -> the RESOLVED_BUNDLE_ID the step exports, and whether it rewrote app.json
  def resolve(wanted_name, listed)
    FakeAppStoreConnect.apps = listed
    Dir.mktmpdir do |dir|
      Dir.chdir(dir) do
        Dir.mkdir("project")
        File.write("project/app.json", { "expo" => { "name" => wanted_name, "ios" => { "bundleIdentifier" => OWN } } }.to_json)
        github_env = File.join(dir, "env")
        saved = ENV.to_h
        ENV.update(
          "APP_STORE_CONNECT_KEY_ID" => "KEY", "APP_STORE_CONNECT_ISSUER_ID" => "ISSUER", "APP_BUNDLE_ID" => OWN,
          "XCODE_SCHEME" => wanted_name, "APP_STORE_CONNECT_API_KEY" => OpenSSL::PKey::EC.generate("prime256v1").to_pem,
          "GITHUB_ENV" => github_env, "GITHUB_WORKSPACE" => ROOT
        )
        begin
          out = StringIO.new
          $stdout = out
          eval(reconcile_script, TOPLEVEL_BINDING, "build-ipa.yml (Register bundle ID)") # rubocop:disable Security/Eval
        ensure
          $stdout = STDOUT
          ENV.replace(saved)
        end
        [File.read(github_env)[/RESOLVED_BUNDLE_ID=(.+)/, 1], JSON.parse(File.read("project/app.json"))["expo"]["ios"]["bundleIdentifier"], out.string]
      end
    end
  end

  def test_an_app_whose_name_only_contains_ours_is_not_adopted
    # The job that was wrongly reused: asking for "App" listed "Ios Appp test55" (another job's app).
    resolved, in_app_json, _ = resolve("App", [app("Ios Appp test55", OTHER_JOB)])
    assert_equal OWN, resolved
    assert_equal OWN, in_app_json
  end

  def test_another_jobs_app_with_exactly_our_name_is_not_adopted
    resolved, = resolve("Shop", [app("Shop", OTHER_JOB)])
    assert_equal OWN, resolved
  end

  def test_an_imported_app_that_really_owns_the_name_is_still_adopted
    resolved, in_app_json, out = resolve("Shop", [app("shop", "com.acme.shop")])
    assert_equal "com.acme.shop", resolved
    assert_equal "com.acme.shop", in_app_json
    assert_match(/Rebuilding against the existing app/, out)
  end

  def test_a_partial_name_of_an_imported_app_is_not_adopted
    resolved, = resolve("Shop", [app("Shop Helper", "com.acme.helper")])
    assert_equal OWN, resolved
  end

  def test_no_app_by_that_name_changes_nothing
    resolved, = resolve("Shop", [])
    assert_equal OWN, resolved
  end
end
