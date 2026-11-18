# Is a `match` failure the "the certificate in our stored signing bundle is gone from the Developer Portal" one?
#
# That happens when a certificate is revoked or deleted on Apple's site while the job owner's persisted bundle still holds it. `match`
# then refuses to mint a replacement: it still finds the (now invalid) cert file and fails. The Fastfile's recovery wipes the stale
# certs/profiles and retries once, but it has to recognise the failure first.
#
# The sentence "Certificate '<id>' (stored in your storage) is not available on the Developer Portal" is only LOGGED by match
# (UI.error, fastlane 2.236.1 match/spaceship_ensure.rb). The exception it raises right after carries a different message:
# "To reset the certificates of your Apple account, you can use the `fastlane match nuke` feature ...". The recovery looked for the
# logged sentence in the exception, never found it, and re-raised, so a stale bundle always failed the build.
#
# Not every "match nuke" message is this one: match also raises "Could not create a new certificate as you reached the maximum number
# of certificates ... `fastlane match nuke`" (the team is at Apple's limit). Wiping the bundle and retrying cannot fix that, so it is
# not matched here.
module StaleSigning
  STALE_CERT_MARKERS = [
    "To reset the certificates of your Apple account", # what match raises after logging that the stored certificate is gone
    "is not available on the Developer Portal",        # the logged sentence, in case a fastlane version puts it in the exception
  ].freeze

  def self.stale_cert_error?(message)
    text = message.to_s
    STALE_CERT_MARKERS.any? { |marker| text.include?(marker) }
  end
end
