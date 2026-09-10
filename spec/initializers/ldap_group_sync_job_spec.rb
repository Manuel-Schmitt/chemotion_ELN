# frozen_string_literal: true

require 'rails_helper'

describe LdapGroupSyncJob do
  describe '.enabled?' do
    it 'is true when host, base and at least one mapping are present' do
      stub_const('ENV', ENV.to_hash.merge('LDAP_HOST' => 'ldap.example.org', 'LDAP_BASE' => 'dc=example,dc=org'))
      allow(described_class).to receive(:mappings).and_return([{ ad_group_dn: 'x', chemotion_group: 'y' }])

      expect(described_class.enabled?).to be(true)
    end

    it 'is false when there are no mappings' do
      stub_const('ENV', ENV.to_hash.merge('LDAP_HOST' => 'ldap.example.org', 'LDAP_BASE' => 'dc=example,dc=org'))
      allow(described_class).to receive(:mappings).and_return([])

      expect(described_class.enabled?).to be(false)
    end

    it 'is false when LDAP_HOST/LDAP_BASE are missing' do
      stub_const('ENV', ENV.to_hash.merge('LDAP_HOST' => nil, 'LDAP_BASE' => nil))
      allow(described_class).to receive(:mappings).and_return([{ ad_group_dn: 'x', chemotion_group: 'y' }])

      expect(described_class.enabled?).to be(false)
    end
  end

  describe '#perform' do
    let!(:chemists) { create(:group, name_abbreviation: 'chm') }

    before do
      allow(described_class).to receive_messages(
        enabled?: true,
        mappings: [{ ad_group_dn: 'cn=eln-chemists,ou=groups,dc=example,dc=org', chemotion_group: 'chm' }],
      )
    end

    it 'adds a Person who is an AD member but not yet in the Chemotion group' do
      person = create(:person, name_abbreviation: 'jd')
      allow(LdapMembershipCheck).to receive(:members).and_return(Set['jd'])

      described_class.new.perform

      expect(chemists.reload.users).to include(person)
    end

    it 'removes a Person who is in the Chemotion group but no longer an AD member' do
      person = create(:person, name_abbreviation: 'jd')
      chemists.users << person
      allow(LdapMembershipCheck).to receive(:members).and_return(Set['someoneelse'])

      described_class.new.perform

      expect(chemists.reload.users).not_to include(person)
    end

    it 'leaves membership untouched for a Person who is already an AD member' do
      person = create(:person, name_abbreviation: 'jd')
      chemists.users << person
      allow(LdapMembershipCheck).to receive(:members).and_return(Set['jd'])

      described_class.new.perform

      expect(chemists.reload.users).to include(person)
    end

    it 'skips (and does not raise) when the mapped Chemotion Group does not exist' do
      allow(described_class).to receive(:mappings).and_return(
        [{ ad_group_dn: 'cn=eln-ghosts,ou=groups,dc=example,dc=org', chemotion_group: 'nope' }],
      )
      allow(LdapMembershipCheck).to receive(:members).and_return(Set['jd'])

      expect { described_class.new.perform }.not_to raise_error
    end

    it 'skips (and does not empty the group) when the AD group resolves to zero members' do
      person = create(:person, name_abbreviation: 'jd')
      chemists.users << person
      allow(LdapMembershipCheck).to receive(:members).and_return(Set.new)

      described_class.new.perform

      expect(chemists.reload.users).to include(person)
    end

    it 'logs and does not raise when the LDAP connection fails' do
      allow(LdapMembershipCheck).to receive(:members).and_raise(Net::LDAP::Error, 'connection refused')

      expect { described_class.new.perform }.not_to raise_error
    end
  end
end
