# frozen_string_literal: true

# Registers a Smart Proxy with content in Foreman through its API, with
# requests signed by the Foreman OAuth consumer (OAuth 1.0a, HMAC-SHA1,
# RFC 5849), as foremanctl does with theforeman.foreman.smart_proxy. Two
# records, linked by Katello through the host name of their URLs:
#
#   PULP_NAME   Pulp's smart_proxy API, feature Pulpcore in mirror mode
#   PROXY_NAME  foreman-proxy, features PROXY_FEATURES
#
# Each is created or updated (URL, organizations and locations added to the
# existing ones). When no record has the name yet but one has the name derived
# from PREVIOUS_NAME (the Smart Proxy got a new host name, for example after a
# restore into another namespace), that record is renamed, so that hosts keep
# it as their registration and content source. Then each is refreshed until Foreman reports the expected features
# and, with the Script feature, has stored the SSH public key.
# Then the lifecycle environments named in LIFECYCLE_ENVIRONMENTS are added to
# the Pulp record in each of its organizations. Every step is idempotent.
# Retries until TIMEOUT_SECONDS, because the pods of this release and Foreman
# may still be starting. Never prints the OAuth secret.

require 'base64'
require 'json'
require 'net/http'
require 'openssl'
require 'securerandom'
require 'uri'

FOREMAN_URL = ENV.fetch('FOREMAN_URL').chomp('/')
CA_FILE = ENV.fetch('FOREMAN_CA_FILE')
CONSUMER_KEY = ENV.fetch('OAUTH_CONSUMER_KEY')
CONSUMER_SECRET = ENV.fetch('OAUTH_CONSUMER_SECRET')
DOWNLOAD_POLICY = ENV.fetch('DOWNLOAD_POLICY', '')
PREVIOUS_NAME = ENV.fetch('PREVIOUS_NAME', '')
PULP = { name: ENV.fetch('PULP_NAME'), url: ENV.fetch('PULP_URL'), features: %w[Pulpcore],
         previous: PREVIOUS_NAME.empty? ? nil : "#{PREVIOUS_NAME}-pulp" }.freeze
PROXY = { name: ENV.fetch('PROXY_NAME'), url: ENV.fetch('PROXY_URL'),
          features: ENV.fetch('PROXY_FEATURES').split, previous: PREVIOUS_NAME.empty? ? nil : PREVIOUS_NAME }.freeze
ORGANIZATIONS = JSON.parse(ENV.fetch('ORGANIZATIONS', '[]'))
LOCATIONS = JSON.parse(ENV.fetch('LOCATIONS', '[]'))
ENVIRONMENTS = JSON.parse(ENV.fetch('LIFECYCLE_ENVIRONMENTS', '[]'))
DEADLINE = Time.now + Integer(ENV.fetch('TIMEOUT_SECONDS', '600'), 10)
PER_PAGE = '1000'

# Worth retrying: Foreman or this Smart Proxy is not ready yet.
class NotReady < StandardError; end
# Retrying does not help.
class Fatal < StandardError; end

abort('OAUTH_CONSUMER_KEY or OAUTH_CONSUMER_SECRET is empty') if CONSUMER_KEY.empty? || CONSUMER_SECRET.empty?

# RFC 3986 percent-encoding, as OAuth requires.
def escape(value)
  value.to_s.b.gsub(/[^A-Za-z0-9\-._~]/) { |char| format('%%%02X', char.ord) }
end

def authorization(method, uri, query)
  oauth = {
    'oauth_consumer_key' => CONSUMER_KEY,
    'oauth_nonce' => SecureRandom.hex(16),
    'oauth_signature_method' => 'HMAC-SHA1',
    'oauth_timestamp' => Time.now.to_i.to_s,
    'oauth_version' => '1.0'
  }
  port = uri.port == uri.default_port ? '' : ":#{uri.port}"
  parameters = (oauth.to_a + query.to_a).map { |key, value| [escape(key), escape(value)] }.sort
  base = [method, "#{uri.scheme}://#{uri.host.downcase}#{port}#{uri.path}",
          parameters.map { |key, value| "#{key}=#{value}" }.join('&')].map { |part| escape(part) }.join('&')
  signature = Base64.strict_encode64(OpenSSL::HMAC.digest('SHA1', "#{escape(CONSUMER_SECRET)}&", base))
  'OAuth ' + oauth.merge('oauth_signature' => signature).map { |key, value| %(#{key}="#{escape(value)}") }.join(', ')
end

def api(method, path, body: nil, query: {})
  uri = URI("#{FOREMAN_URL}#{path}")
  uri.query = query.map { |key, value| "#{escape(key)}=#{escape(value)}" }.join('&') unless query.empty?
  request = Net::HTTP.const_get(method.capitalize).new(uri)
  request['Accept'] = 'application/json'
  # Foreman answers 415 to a PUT without it, even without a body.
  request['Content-Type'] = 'application/json'
  request['Authorization'] = authorization(method, uri, query)
  request.body = JSON.generate(body || {}) unless method == 'GET'
  http = Net::HTTP.new(uri.host, uri.port)
  http.use_ssl = true
  http.ca_file = CA_FILE
  http.verify_mode = OpenSSL::SSL::VERIFY_PEER
  http.open_timeout = 10
  http.read_timeout = 120
  response = http.request(request)
  code = response.code.to_i
  if code == 401
    raise Fatal, "#{method} #{path}: 401. Foreman did not accept the OAuth signature: compare the OAuth " \
                 'consumer in the bundle Secret with foreman.oauth of the Foreman server'
  end
  raise NotReady, "#{method} #{path}: #{code} #{response.body.to_s[0, 1000]}" unless code.between?(200, 299)

  response.body.to_s.empty? ? {} : JSON.parse(response.body)
end

def ids(kind, names)
  all = api('GET', "/api/#{kind}", query: { 'per_page' => PER_PAGE })['results']
  return all.map { |item| item['id'] } if names.empty?

  names.map do |name|
    found = all.find { |item| [item['name'], item['title']].include?(name) }
    raise Fatal, "#{kind}: #{name.inspect} not found" unless found

    found['id']
  end
end

def find_proxy(name)
  search = { 'search' => %(name = "#{name}"), 'per_page' => PER_PAGE }
  api('GET', '/api/smart_proxies', query: search)['results'].find { |proxy| proxy['name'] == name }
end

def register(record, organization_ids, location_ids, extra = {})
  found = find_proxy(record[:name])
  action = 'Updated'
  if !found && record[:previous] && (found = find_proxy(record[:previous]))
    action = "Renamed from #{record[:previous]}:"
  end
  if found
    current = api('GET', "/api/smart_proxies/#{found['id']}")
    attributes = {
      'name' => record[:name],
      'url' => record[:url],
      'organization_ids' => current.fetch('organizations', []).map { |o| o['id'] } | organization_ids,
      'location_ids' => current.fetch('locations', []).map { |l| l['id'] } | location_ids
    }.merge(extra)
    id = api('PUT', "/api/smart_proxies/#{found['id']}", body: { 'smart_proxy' => attributes })['id']
  else
    attributes = { 'name' => record[:name], 'url' => record[:url], 'organization_ids' => organization_ids,
                   'location_ids' => location_ids }.merge(extra)
    id = api('POST', '/api/smart_proxies', body: { 'smart_proxy' => attributes })['id']
    action = 'Created'
  end
  api('PUT', "/api/smart_proxies/#{id}/refresh")
  proxy = api('GET', "/api/smart_proxies/#{id}")
  features = proxy.fetch('features', []).map { |feature| feature['name'] }
  missing = record[:features] - features
  raise NotReady, "#{record[:name]}: features #{features.sort.inspect} lack #{missing.inspect}" unless missing.empty?
  # foreman_remote_execution stores the SSH public key on refresh, not on
  # create. Global Registration installs it on hosts from there.
  if record[:features].include?('Script') && proxy['remote_execution_pubkey'].to_s.strip.empty?
    raise NotReady, "#{record[:name]}: Foreman has not stored the Remote Execution SSH public key yet"
  end

  puts "#{action} Smart Proxy #{record[:name]} (id #{id}) at #{record[:url]}"
  puts "  features: #{features.sort.join(', ')}"
  puts "  organizations: #{proxy.fetch('organizations', []).map { |o| o['name'] }.sort.join(', ')}"
  proxy
end

def assign_environments(proxy)
  return if ENVIRONMENTS.empty?

  capsule = "/katello/api/capsules/#{proxy['id']}/content"
  organization_ids = proxy.fetch('organizations', []).map { |o| o['id'] }
  assigned = lambda do
    api('GET', "#{capsule}/lifecycle_environments", query: { 'per_page' => PER_PAGE })['results']
  end
  # Katello lists attached environments as available too.
  attached = assigned.call.map { |environment| environment['id'] }
  available = api('GET', "#{capsule}/available_lifecycle_environments", query: { 'per_page' => PER_PAGE })['results']
  available.each do |environment|
    next unless ENVIRONMENTS.include?(environment['name'])
    next unless organization_ids.include?(environment.dig('organization', 'id'))
    next if attached.include?(environment['id'])

    api('POST', "#{capsule}/lifecycle_environments", body: { 'environment_id' => environment['id'] })
  end
  puts "  lifecycle environments: #{assigned.call.map { |e| "#{e.dig('organization', 'name')}/#{e['name']}" }.sort.join(', ')}"
end

begin
  organization_ids = ids('organizations', ORGANIZATIONS)
  location_ids = ids('locations', LOCATIONS)
  extra = DOWNLOAD_POLICY.empty? ? {} : { 'download_policy' => DOWNLOAD_POLICY }
  pulp = register(PULP, organization_ids, location_ids, extra)
  register(PROXY, organization_ids, location_ids)
  assign_environments(pulp)
rescue NotReady, SystemCallError, IOError, Timeout::Error, OpenSSL::SSL::SSLError, SocketError,
       JSON::ParserError => e
  abort("Not registered: #{e.class}: #{e.message}") if Time.now > DEADLINE

  warn "Not registered yet: #{e.class}: #{e.message}"
  sleep 10
  retry
rescue Fatal => e
  abort(e.message)
end
