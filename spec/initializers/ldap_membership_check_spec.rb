# frozen_string_literal: true

require 'rails_helper'

describe LdapMembershipCheck do
  describe '.enabled?' do
    it 'is true when host and base are present' do
      stub_const('ENV', ENV.to_hash.merge('LDAP_HOST' => 'ldap.example.org', 'LDAP_BASE' => 'dc=example,dc=org'))

      expect(described_class.enabled?).to be(true)
    end

    it 'is false when host or base is missing' do
      stub_const('ENV', ENV.to_hash.merge('LDAP_HOST' => 'ldap.example.org', 'LDAP_BASE' => nil))

      expect(described_class.enabled?).to be(false)
    end
  end

  describe '.member?' do
    let(:connection) { instance_double(Net::LDAP) }
    let(:group_dn) { 'cn=eln-users,ou=groups,dc=example,dc=org' }

    before do
      stub_const('ENV', ENV.to_hash.merge('LDAP_HOST' => 'ldap.example.org', 'LDAP_BASE' => 'dc=example,dc=org'))
      allow(Net::LDAP).to receive(:new).and_return(connection)
    end

    it 'matches uid against the given group, resolving nested groups' do
      allow(connection).to receive(:search).and_return([{ dn: 'cn=jdoe,dc=example,dc=org' }])

      expect(described_class.member?(group_dn, 'jdoe')).to be(true)
      expect(connection).to have_received(:search).with(
        base: 'dc=example,dc=org',
        filter: Net::LDAP::Filter.construct(
          '(&(sAMAccountName=jdoe)(objectClass=user)(objectCategory=person)' \
          '(memberOf:1.2.840.113556.1.4.1941:=cn=eln-users,ou=groups,dc=example,dc=org))',
        ),
        attributes: ['dn'],
        paged_searches: false,
      )
    end

    it 'returns false when no entry matches' do
      allow(connection).to receive(:search).and_return([])

      expect(described_class.member?(group_dn, 'jdoe')).to be(false)
    end

    it 'escapes LDAP special characters in uid to prevent filter injection' do
      allow(connection).to receive(:search).and_return([])

      described_class.member?(group_dn, 'jdoe)(uid=*')

      expect(connection).to have_received(:search).with(
        hash_including(filter: Net::LDAP::Filter.construct(
          '(&(sAMAccountName=jdoe\29\28uid=\2A)(objectClass=user)(objectCategory=person)' \
          '(memberOf:1.2.840.113556.1.4.1941:=cn=eln-users,ou=groups,dc=example,dc=org))',
        )),
      )
    end

    it 'returns false without querying when uid is blank' do
      allow(connection).to receive(:search)

      expect(described_class.member?(group_dn, '')).to be(false)
      expect(connection).not_to have_received(:search)
    end

    it 'returns false without querying when group_dn is blank' do
      allow(connection).to receive(:search)

      expect(described_class.member?('', 'jdoe')).to be(false)
      expect(connection).not_to have_received(:search)
    end

    it 'uses LDAP_UID_ATTRIBUTE instead of the sAMAccountName default when configured' do
      stub_const('ENV', ENV.to_hash.merge('LDAP_UID_ATTRIBUTE' => 'uid'))
      allow(connection).to receive(:search).and_return([])

      described_class.member?(group_dn, 'jdoe')

      expect(connection).to have_received(:search).with(
        hash_including(filter: Net::LDAP::Filter.construct(
          '(&(uid=jdoe)(objectClass=user)(objectCategory=person)' \
          '(memberOf:1.2.840.113556.1.4.1941:=cn=eln-users,ou=groups,dc=example,dc=org))',
        )),
      )
    end

    it 'raises when the search fails without itself raising (e.g. a bind failure)' do
      operation_result = Struct.new(:message).new('Invalid Credentials')
      allow(connection).to receive_messages(search: nil, get_operation_result: operation_result)

      expect { described_class.member?(group_dn, 'jdoe') }.to raise_error(Net::LDAP::Error, 'Invalid Credentials')
    end
  end

  describe '.members' do
    let(:connection) { instance_double(Net::LDAP) }
    let(:group_dn) { 'cn=eln-users,ou=groups,dc=example,dc=org' }

    before do
      stub_const('ENV', ENV.to_hash.merge('LDAP_HOST' => 'ldap.example.org', 'LDAP_BASE' => 'dc=example,dc=org'))
      allow(Net::LDAP).to receive(:new).and_return(connection)
    end

    it 'runs a single wildcard, paged query and returns the downcased uid of every match' do
      allow(connection).to receive(:search).and_return(
        [{ 'sAMAccountName' => ['JDoe'] }, { 'sAMAccountName' => ['ASmith'] }],
      )

      expect(described_class.members(group_dn)).to eq(Set['jdoe', 'asmith'])
      expect(connection).to have_received(:search).with(
        base: 'dc=example,dc=org',
        filter: Net::LDAP::Filter.construct(
          '(&(objectClass=user)(objectCategory=person)' \
          '(memberOf:1.2.840.113556.1.4.1941:=cn=eln-users,ou=groups,dc=example,dc=org))',
        ),
        attributes: ['sAMAccountName'],
        paged_searches: true,
      )
    end

    it 'returns an empty set when an entry lacks the uid attribute' do
      allow(connection).to receive(:search).and_return([{ 'sAMAccountName' => [] }, {}])

      expect(described_class.members(group_dn)).to eq(Set.new)
    end

    it 'returns an empty set when no entry matches' do
      allow(connection).to receive(:search).and_return([])

      expect(described_class.members(group_dn)).to eq(Set.new)
    end

    it 'raises when the search fails without itself raising (e.g. a bind failure), rather than ' \
       'returning an empty set that would read as "nobody is a member"' do
      operation_result = Struct.new(:message).new('Invalid Credentials')
      allow(connection).to receive_messages(search: nil, get_operation_result: operation_result)

      expect { described_class.members(group_dn) }.to raise_error(Net::LDAP::Error, 'Invalid Credentials')
    end
  end
end
