# ruby test/appstore_name_match_test.rb
require "minitest/autorun"
require_relative "../fastlane/appstore_name_match"

class AppStoreNameMatchTest < Minitest::Test
  OWN = "com.frontendx.job-8229fa64-49b4-490f-9857-841b4f083204".freeze
  OTHER_JOB = "com.frontendx.job-2d31769a-1e7d-42bc-b2dc-8408f6d1bded".freeze

  def reusable?(name, bundle, wanted = "App", own = OWN)
    AppStoreNameMatch.reusable?(found_name: name, found_bundle_id: bundle, wanted_name: wanted, own_bundle_id: own)
  end

  def test_a_name_that_only_contains_the_request_is_not_the_same_app
    refute reusable?("Ios Appp test55", "com.acme.shop") # the job that was wrongly reused: "App" vs "Ios Appp test55"
    refute reusable?("App Store Helper", "com.acme.helper")
    refute reusable?("Application", "com.acme.application")
  end

  def test_the_exact_name_is_the_same_app_whatever_its_case_or_spacing
    assert reusable?("App", "com.acme.app")
    assert reusable?("  my   shop ", "com.acme.shop", "My Shop")
  end

  def test_the_name_cut_to_apples_limit_matches_too
    wanted = "A very long application name that goes past thirty characters"
    assert reusable?(wanted[0, 30], "com.acme.long", wanted)
  end

  def test_another_frontendx_jobs_app_is_never_reused_even_with_the_same_name
    refute reusable?("App", OTHER_JOB)
    refute reusable?("Ios Appp test55", OTHER_JOB, "Ios Appp test55")
  end

  def test_our_own_bundle_id_or_no_bundle_id_is_not_a_drifted_app
    refute reusable?("App", OWN)
    refute reusable?("App", "")
    refute reusable?("App", nil)
  end

  def test_nothing_to_compare
    refute reusable?(nil, "com.acme.app")
    refute reusable?("", "com.acme.app")
  end
end
