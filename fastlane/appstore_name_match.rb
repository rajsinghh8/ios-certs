# Which existing App Store Connect app, if any, may stand in for the app we are about to create or build, when no app has OUR bundle ID.
#
# The name lookup exists for an app that is already live under a bundle ID that drifted from the one we derived (an imported repository):
# App Store display names are globally unique, so adding a build to the app that owns the name beats minting a duplicate. It must not
# reuse just any app the lookup returns:
#
#   * App Store Connect's `filter[name]` is not an exact match, so a request for "App" also returns "Ios Appp test55". Only an app whose
#     name IS the requested name (ignoring case and spacing) is the same app.
#   * An app with a `com.frontendx.job-<id>` bundle ID was created for another FrontendX job (ours is looked up by bundle ID first and
#     is not this one). Two projects must never share one App Store app. If the name is really taken, `produce` says so (APP_NAME_TAKEN)
#     and the member renames the project; if it is free, the new project gets its own app.
module AppStoreNameMatch
  JOB_BUNDLE_PREFIX = "com.frontendx.job-".freeze
  DISPLAY_NAME_LIMIT = 30 # produce registers the name cut to this length

  def self.same_name?(found_name, wanted_name)
    found = normalize(found_name)
    return false if found.empty?

    wanted = normalize(wanted_name)
    found == wanted || found == normalize(wanted_name.to_s[0, DISPLAY_NAME_LIMIT])
  end

  def self.reusable?(found_name:, found_bundle_id:, wanted_name:, own_bundle_id:)
    bundle = found_bundle_id.to_s
    return false if bundle.empty? || bundle == own_bundle_id
    return false if bundle.start_with?(JOB_BUNDLE_PREFIX)

    same_name?(found_name, wanted_name)
  end

  def self.normalize(value)
    value.to_s.strip.gsub(/\s+/, " ").downcase
  end
end
