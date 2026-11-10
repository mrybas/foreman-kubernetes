# frozen_string_literal: true

# Run with `bin/rails runner` in the Foreman image. Register the Smart Proxy
# in Foreman, or update its URL, then always refresh it: the refresh fetches
# the features and, for Remote Execution, the proxy's SSH public key, which
# Foreman hands to hosts at registration. Retries until TIMEOUT_SECONDS
# because the proxy may still be starting. The proxy is added to every
# organization and location so remote execution can use it for any host.
# Existing assignments are kept. Talks to the database directly, so no
# Foreman user or password is needed.

NAME = ENV.fetch('PROXY_NAME')
URL = ENV.fetch('PROXY_URL')
FEATURES = ENV.fetch('PROXY_FEATURES').split
DEADLINE = Time.now + Integer(ENV.fetch('TIMEOUT_SECONDS', '600'), 10)

class NotReady < StandardError; end

def register
  User.as_anonymous_admin do
    proxy = SmartProxy.unscoped.find_or_initialize_by(name: NAME)
    created = proxy.new_record?
    proxy.url = URL
    proxy.organization_ids = proxy.organization_ids | Organization.unscoped.pluck(:id)
    proxy.location_ids = proxy.location_ids | Location.unscoped.pluck(:id)
    raise NotReady, "cannot save: #{proxy.errors.full_messages.join('; ')}" unless proxy.save

    errors = proxy.refresh
    raise NotReady, "cannot refresh: #{errors.full_messages.join('; ')}" if errors.any?

    proxy.reload
    features = proxy.features.pluck(:name)
    missing = FEATURES - features
    raise NotReady, "features #{features.sort.inspect} lack #{missing.inspect}" unless missing.empty?

    # Stored by refresh; Foreman's API shows it as remote_execution_pubkey.
    if FEATURES.include?('Script') && proxy.has_attribute?(:pubkey) && proxy[:pubkey].blank?
      raise NotReady, 'no Remote Execution SSH public key stored'
    end

    puts "#{created ? 'Created' : 'Updated'} Smart Proxy #{NAME} (id #{proxy.id}) at #{URL}"
    puts "Features: #{features.sort.join(', ')}"
  end
end

begin
  register
rescue NotReady, SystemCallError, IOError, Timeout::Error, OpenSSL::SSL::SSLError, SocketError => e
  raise if Time.now > DEADLINE

  warn "Not registered yet: #{e.message}"
  sleep 10
  retry
end
