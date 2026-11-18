# ruby test/stale_signing_test.rb
require "minitest/autorun"
require_relative "../fastlane/stale_signing"

class StaleSigningTest < Minitest::Test
  # The exact exception message of the failed build (fastlane 2.236.1): the "not available" sentence was only logged.
  RAISED_BY_MATCH = "To reset the certificates of your Apple account, you can use the `fastlane match nuke` feature, " \
                    "more information on https://docs.fastlane.tools/actions/match/".freeze

  def test_the_message_match_raises_for_a_stored_certificate_that_is_gone_is_recognised
    assert StaleSigning.stale_cert_error?(RAISED_BY_MATCH)
  end

  def test_the_logged_sentence_is_recognised_too
    assert StaleSigning.stale_cert_error?("Certificate 'RRT32FZN9R' (stored in your storage) is not available on the Developer Portal")
  end

  def test_the_team_being_at_the_certificate_limit_is_not_a_stale_bundle
    refute StaleSigning.stale_cert_error?("Could not create a new certificate as you reached the maximum number of certificates for this account. " \
                                          "You can use the `fastlane match nuke` command to revoke your existing certificates.")
    refute StaleSigning.stale_cert_error?("Could not create another Distribution certificate, reached the maximum number of available Distribution certificates.")
  end

  def test_other_failures_are_not_a_stale_bundle
    refute StaleSigning.stale_cert_error?("Invalid username and password combination")
    refute StaleSigning.stale_cert_error?("")
    refute StaleSigning.stale_cert_error?(nil)
  end
end
